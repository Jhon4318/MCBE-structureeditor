#!/usr/bin/env python3
"""Qt6 front-end for the MCBE Structure Editor. NBT operations stay in the legacy-tested core."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import mcstructure_merger_v2_features_bundle as core
import numpy as np
from PySide6.QtCore import Qt, QTimer, QSize, QRectF
from PySide6.QtGui import QAction, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem, QToolBar,
    QDockWidget, QFormLayout, QSpinBox, QComboBox, QPushButton, QLabel,
    QFileDialog, QMessageBox, QStatusBar, QStackedWidget,
    QFrame, QLineEdit, QInputDialog, QMenu, QCheckBox, QAbstractItemView
)

APP_NAME = "MCBE Structure Editor"
OVERLAP_LABELS = {
    "last_defined": "後の構造物を優先（未設定セルは保持）",
    "first_non_air": "最初の構造物を優先（空気以外）",
    "last_including_void": "後の構造物で領域全体を置換",
}
DARK_QSS = """
QMainWindow,QWidget { background:#171a21; color:#e7eaf0; font-family:'Segoe UI','Yu Gothic UI'; font-size:10pt; }
QToolBar { background:#202530; border:0; spacing:6px; padding:8px; }
QToolButton,QPushButton { background:#2a3240; color:#edf1f7; border:1px solid #3b4658; border-radius:7px; padding:7px 12px; }
QToolButton:hover,QPushButton:hover { background:#354257; border-color:#6585b5; }
QPushButton:disabled { color:#77808e; background:#222833; }
QListWidget,QTreeWidget,QLineEdit,QSpinBox,QComboBox { background:#202630; border:1px solid #384353; border-radius:6px; padding:5px; selection-background-color:#315d91; }
QListWidget::item { padding:8px; border-radius:5px; }
QListWidget::item:selected { background:#29486c; }
QDockWidget::title { background:#202530; padding:9px; font-weight:600; }
QStatusBar { background:#202530; color:#aeb8c8; }
QFrame#Panel { background:#202630; border:1px solid #303b4b; border-radius:9px; }
QLabel#Heading { font-size:12pt; font-weight:600; }
"""
LIGHT_QSS = """
QMainWindow,QWidget { background:#f3f5f8; color:#202633; font-family:'Segoe UI','Yu Gothic UI'; font-size:10pt; }
QToolBar { background:#ffffff; border:0; spacing:6px; padding:8px; }
QToolButton,QPushButton { background:#ffffff; color:#202633; border:1px solid #d7dde7; border-radius:7px; padding:7px 12px; }
QToolButton:hover,QPushButton:hover { background:#eaf1fb; border-color:#8aa9d4; }
QPushButton:disabled { color:#a1a8b2; background:#f4f5f7; }
QListWidget,QTreeWidget,QLineEdit,QSpinBox,QComboBox { background:#ffffff; border:1px solid #d7dde7; border-radius:6px; padding:5px; selection-background-color:#cfe0f8; }
QListWidget::item { padding:8px; border-radius:5px; }
QListWidget::item:selected { background:#dce9fa; }
QDockWidget::title { background:#ffffff; padding:9px; font-weight:600; }
QStatusBar { background:#ffffff; color:#667085; }
QFrame#Panel { background:#ffffff; border:1px solid #dfe4ec; border-radius:9px; }
QLabel#Heading { font-size:12pt; font-weight:600; }
"""

def block_color(name: str) -> QColor:
    """Stable, deliberately approximate material colours; no resource-pack lookup."""
    n = name.lower()
    known = (
        ("air", (0, 0, 0)), ("stone", (120, 122, 126)), ("deepslate", (75, 78, 84)),
        ("dirt", (133, 96, 65)), ("grass", (103, 151, 72)), ("leaves", (67, 125, 64)),
        ("log", (133, 101, 67)), ("wood", (164, 127, 82)), ("planks", (177, 139, 88)),
        ("sand", (218, 204, 142)), ("glass", (155, 202, 205)), ("water", (65, 112, 194)),
        ("lava", (225, 105, 35)), ("snow", (225, 235, 242)), ("ice", (142, 193, 218)),
        ("brick", (151, 83, 69)), ("wool", (190, 190, 190)), ("concrete", (145, 145, 145)),
        ("gold", (235, 190, 45)), ("iron", (190, 195, 198)), ("copper", (181, 112, 75)),
        ("diamond", (70, 205, 190)), ("emerald", (60, 190, 105)), ("redstone", (170, 45, 45)),
        ("coal", (52, 54, 58)), ("obsidian", (54, 42, 78)), ("netherrack", (133, 61, 57)),
        ("glowstone", (218, 176, 90)), ("tnt", (181, 60, 52)), ("chest", (155, 105, 54)),
        ("furnace", (102, 102, 102)), ("dispenser", (116, 116, 116)), ("piston", (148, 128, 98)),
        ("bed", (165, 64, 75)), ("wheat", (184, 164, 70)), ("flower", (200, 110, 140)),
    )
    for token, rgb in known:
        if token in n:
            return QColor(*rgb)
    h = int(hashlib.md5(n.encode("utf-8")).hexdigest()[:6], 16)
    return QColor(75 + (h & 0x7f), 75 + ((h >> 8) & 0x7f), 75 + ((h >> 16) & 0x7f))

def palette_names(src):
    result = []
    for entry in src.block_palette:
        tag = entry.value.get("name") if entry.tag_id == 10 else None
        result.append(tag.value if tag and tag.tag_id == 8 else "unknown")
    return result

class PreviewWidget(QOpenGLWidget):
    """Qt OpenGL-backed viewport; QPainter batches exposed voxel faces into paths."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setMinimumSize(420, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.mode = "3d"
        self._drag = None
        self._pan = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._mesh_cache = {}
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.PartialUpdate)

    def invalidate(self):
        self._mesh_cache.clear()
        self.update()

    def _structure_mesh(self, row):
        key = (row["id"], row["rotation"], row["mirror_x"], row["mirror_z"])
        if key in self._mesh_cache:
            return self._mesh_cache[key]
        src = core.transform_loaded_structure(row["loaded"], row["rotation"], row["mirror_x"], row["mirror_z"])
        sx, sy, sz = src.size
        names = palette_names(src)
        air = {i for i, n in enumerate(names) if n == "minecraft:air" or n.endswith(":air")}
        palette_solid = np.ones(len(names), dtype=np.bool_)
        if air:
            palette_solid[list(air)] = False
        primary = np.frombuffer(src.layers[0], dtype=np.int32)
        secondary = np.frombuffer(src.layers[1], dtype=np.int32) if len(src.layers) > 1 else None
        pvalid = (primary >= 0) & (primary < len(names))
        solid_mask = pvalid & palette_solid[np.maximum(primary, 0)]
        if secondary is not None:
            svalid = (secondary >= 0) & (secondary < len(names))
            solid_mask |= svalid & palette_solid[np.maximum(secondary, 0)]
        solid_count = int(np.count_nonzero(solid_mask))
        stride = max(1, int(math.ceil(solid_count / 140000)))
        candidates = np.flatnonzero(solid_mask[::stride]).astype(np.int64) * stride
        def block_at(index):
            if index < 0 or index >= len(primary):
                return -1
            if secondary is not None:
                value = int(secondary[index])
                if 0 <= value < len(names) and palette_solid[value]:
                    return value
            value = int(primary[index])
            return value if 0 <= value < len(names) and palette_solid[value] else -1
        mesh = []
        for raw_idx in candidates:
            idx = int(raw_idx)
            pidx = block_at(idx)
            if pidx < 0:
                continue
            x = idx // (sy * sz); y = (idx // sz) % sy; z = idx % sz
            c = block_color(names[pidx])
            # Faces are emitted only when the adjacent voxel is empty.
            if y + 1 >= sy or block_at(idx + sz) < 0:
                mesh.append((x, y, z, c, "top"))
            if x + 1 >= sx or block_at(idx + sy * sz) < 0:
                mesh.append((x, y, z, c, "right"))
            if z + 1 >= sz or block_at(idx + 1) < 0:
                mesh.append((x, y, z, c, "left"))
        self._mesh_cache[key] = (src.size, mesh)
        return src.size, mesh

    def paintGL(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor("#10151d") if self.window.theme == "dark" else QColor("#e9eef5")
        p.fillRect(self.rect(), bg)
        if not self.window.rows:
            p.setPen(QColor("#9aa6b8"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "「ファイルを追加」から .mcstructure / ZIP を読み込んでください")
            p.end()
            return
        if self.mode == "top":
            self._paint_top(p)
        else:
            self._paint_iso(p)
        p.end()

    def _paint_top(self, p):
        rows = self.window.rows
        bounds = []
        for r in rows:
            sx, sy, sz = self.window.dimensions(r)
            bounds.append((r["x"], r["z"], sx, sz, r))
        minx = min(v[0] for v in bounds); minz = min(v[1] for v in bounds)
        maxx = max(v[0] + v[2] for v in bounds); maxz = max(v[1] + v[3] for v in bounds)
        spanx = max(1, maxx-minx); spanz = max(1, maxz-minz)
        scale = min((self.width()-80)/spanx, (self.height()-80)/spanz) * self.zoom
        scale = max(0.015, min(scale, 24.0))
        ox = (self.width()-spanx*scale)/2 - minx*scale + self.pan_x
        oy = (self.height()-spanz*scale)/2 - minz*scale + self.pan_y
        p.setPen(QPen(QColor("#667085"), 1))
        grid = 64 * scale
        if grid >= 8:
            gx = math.floor((minx-ox/scale)/64)*64
            gz = math.floor((minz-oy/scale)/64)*64
            p.setPen(QPen(QColor(130,145,165,45), 1))
            for x in range(int(gx), int(maxx+100), 64):
                px=ox+x*scale; p.drawLine(int(px),0,int(px),self.height())
            for z in range(int(gz), int(maxz+100), 64):
                py=oy+z*scale; p.drawLine(0,int(py),self.width(),int(py))
        for x,z,sx,sz,r in bounds:
            rect = (ox+x*scale, oy+z*scale, sx*scale, sz*scale)
            color = QColor("#dc5555") if self.window.overlaps(r) else QColor("#4f8edb")
            p.fillRect(QRectF(*rect), QColor(color.red(),color.green(),color.blue(),55))
            p.setPen(QPen(color, 3 if r["id"] == self.window.selected_id else 2))
            p.drawRect(QRectF(*rect))
            if rect[2] > 70 and rect[3] > 30:
                p.setPen(QColor("#f2f4f7") if self.window.theme=="dark" else QColor("#344054"))
                p.drawText(int(rect[0]+5),int(rect[1]+19),Path(r["display_path"]).name)
        p.setPen(QColor("#aab5c5"))
        p.drawText(12,self.height()-12,f"上面図  |  範囲 {spanx} × {spanz} blocks  |  64ブロックグリッド")

    def _paint_iso(self, p):
        all_mesh = []
        world_min = [10**9,10**9,10**9]; world_max=[-10**9,-10**9,-10**9]
        for row in self.window.rows:
            size, mesh = self._structure_mesh(row)
            sx,sy,sz=size
            world_min=[min(world_min[0],row["x"]),min(world_min[1],row["y"]),min(world_min[2],row["z"])]
            world_max=[max(world_max[0],row["x"]+sx),max(world_max[1],row["y"]+sy),max(world_max[2],row["z"]+sz)]
            all_mesh.append((row,mesh))
        spanx=max(1,world_max[0]-world_min[0]); spany=max(1,world_max[1]-world_min[1]); spanz=max(1,world_max[2]-world_min[2])
        tw=13.0*self.zoom; th=7.0*self.zoom; vh=13.0*self.zoom
        cx=(world_min[0]+world_max[0])/2; cz=(world_min[2]+world_max[2])/2
        center_x=self.width()/2+self.pan_x
        center_y=self.height()/2+self.pan_y + spany*vh*0.25
        def pt(x,y,z):
            return (center_x+(x-z-cx+cz)*tw/2, center_y+(x+z-cx-cz)*th/2-(y-world_min[1])*vh)
        paths={}
        face_order={"left":0,"right":1,"top":2}
        for row,mesh in all_mesh:
            for x,y,z,c,face in mesh:
                wx=x+row["x"]; wy=y+row["y"]; wz=z+row["z"]
                if face=="top":
                    pts=[pt(wx,wy+1,wz),pt(wx+1,wy+1,wz),pt(wx+1,wy+1,wz+1),pt(wx,wy+1,wz+1)]
                    shade=1.12
                elif face=="right":
                    pts=[pt(wx+1,wy,wz),pt(wx+1,wy+1,wz),pt(wx+1,wy+1,wz+1),pt(wx+1,wy,wz+1)]
                    shade=.72
                else:
                    pts=[pt(wx,wy,wz+1),pt(wx,wy+1,wz+1),pt(wx+1,wy+1,wz+1),pt(wx+1,wy,wz+1)]
                    shade=.88
                cc=QColor(min(255,int(c.red()*shade)),min(255,int(c.green()*shade)),min(255,int(c.blue()*shade)))
                path=paths.setdefault(cc.name(),QPainterPath())
                from PySide6.QtCore import QPointF
                path.moveTo(QPointF(*pts[0]))
                for q in pts[1:]: path.lineTo(QPointF(*q))
                path.closeSubpath()
        for color,path in paths.items():
            p.setPen(QPen(QColor("#151a22") if self.window.theme=="dark" else QColor("#64748b"),0.35))
            p.setBrush(QBrush(QColor(color)))
            p.drawPath(path)
        p.setPen(QColor("#aab5c5"))
        p.drawText(12,self.height()-12,f"簡易3D  |  表示面を省略して描画  |  近似マテリアル")

    def mousePressEvent(self,e):
        if e.button()==Qt.MouseButton.RightButton:
            self._pan=(e.position().x(),e.position().y(),self.pan_x,self.pan_y); self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif e.button()==Qt.MouseButton.LeftButton:
            self._drag=(e.position().x(),e.position().y(),self.window.selected_id)
        super().mousePressEvent(e)

    def mouseMoveEvent(self,e):
        if self._pan:
            x,y,px,py=self._pan; self.pan_x=px+e.position().x()-x; self.pan_y=py+e.position().y()-y; self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self,e):
        if self._pan:
            self._pan=None; self.unsetCursor()
        elif self._drag and e.button()==Qt.MouseButton.LeftButton:
            sx,sy,selected=self._drag; dx=e.position().x()-sx; dy=e.position().y()-sy
            if abs(dx)+abs(dy)<5:
                self.window.select_at(e.position().x(),e.position().y(),self.mode)
            elif selected is not None and self.mode=="top":
                row=self.window.row_by_id(selected)
                if row:
                    dim=self.window.dimensions(row)
                    scale=min((self.width()-80)/max(1,self.window.bounds()[2]),(self.height()-80)/max(1,self.window.bounds()[3]))*self.zoom
                    if scale>0:
                        row["x"]=int(round((row["x"]+dx/scale)/64))*64
                        row["z"]=int(round((row["z"]+dy/scale)/64))*64
                        self.window.changed("64ブロック単位で移動しました")
            self._drag=None
        self.update()
        super().mouseReleaseEvent(e)

    def wheelEvent(self,e):
        factor=1.15 if e.angleDelta().y()>0 else 1/1.15
        self.zoom=max(.08,min(8.0,self.zoom*factor)); self.update()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1480,900)
        self.theme="dark"
        self.rows=[]
        self.selected_id=None
        self.overlap_mode="last_defined"
        self._next_id=1
        self._temp_dirs=[]
        self._history=[]; self._history_index=-1
        self.data_dir=core.get_application_data_dir()
        self.autosave_file=self.data_dir/"autosave.json"
        self.sources_dir=self.data_dir/"sources"; self.sources_dir.mkdir(parents=True,exist_ok=True)
        self.save_timer=QTimer(self); self.save_timer.setSingleShot(True); self.save_timer.setInterval(450); self.save_timer.timeout.connect(self.save_autosave)
        self._build_ui()
        self.restore_autosave()
        self.commit_history()
        self.apply_theme()

    def _build_ui(self):
        toolbar=QToolBar("メイン"); toolbar.setIconSize(QSize(18,18)); toolbar.setMovable(False); self.addToolBar(toolbar)
        def action(text,fn): 
            a=QAction(text,self); a.triggered.connect(fn); toolbar.addAction(a); return a
        action("＋ ファイルを追加",self.add_files)
        action("連続配置",self.repeat_dialog)
        action("保存・結合",self.merge)
        toolbar.addSeparator()
        self.undo_action=action("↶ 元に戻す",self.undo); self.redo_action=action("↷ やり直す",self.redo)
        toolbar.addSeparator()
        self.theme_action=action("☼ ライト／ダーク",self.toggle_theme)
        central=QWidget(); root=QVBoxLayout(central); root.setContentsMargins(14,12,14,12); root.setSpacing(10)
        header=QHBoxLayout()
        title=QLabel("Structure Workspace"); title.setObjectName("Heading")
        subtitle=QLabel("Minecraft Bedrock  •  format_version 2")
        header.addWidget(title); header.addSpacing(12); header.addWidget(subtitle); header.addStretch()
        self.mode_combo=QComboBox(); self.mode_combo.addItem("3D プレビュー","3d"); self.mode_combo.addItem("上面図","top")
        self.mode_combo.currentIndexChanged.connect(self.change_preview_mode); header.addWidget(QLabel("表示")); header.addWidget(self.mode_combo)
        root.addLayout(header)
        splitter=QSplitter(Qt.Orientation.Horizontal)
        left=QFrame(); left.setObjectName("Panel"); ll=QVBoxLayout(left); ll.setContentsMargins(12,12,12,12)
        ll.addWidget(QLabel("構造物一覧")); self.list=QListWidget(); self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self.list.currentItemChanged.connect(self.on_current_item)
        ll.addWidget(self.list,1)
        rowbtn=QHBoxLayout()
        for label,fn in [("追加",self.add_files),("削除",self.remove_selected),("↑",lambda:self.reorder(-1)),("↓",lambda:self.reorder(1))]:
            b=QPushButton(label); b.clicked.connect(fn); rowbtn.addWidget(b)
        ll.addLayout(rowbtn)
        self.preview=PreviewWidget(self)
        right=QFrame(); right.setObjectName("Panel"); rl=QVBoxLayout(right); rl.setContentsMargins(14,14,14,14)
        rl.addWidget(QLabel("配置・変形")); form=QFormLayout()
        self.xbox=self._spin(); self.ybox=self._spin(); self.zbox=self._spin()
        form.addRow("X",self.xbox); form.addRow("Y",self.ybox); form.addRow("Z",self.zbox)
        self.rot=QComboBox(); self.rot.addItems(["0°","90°","180°","270°"])
        form.addRow("回転",self.rot)
        self.mx=QCheckBox("X方向ミラー"); self.mz=QCheckBox("Z方向ミラー")
        form.addRow(self.mx); form.addRow(self.mz); rl.addLayout(form)
        self.apply_btn=QPushButton("変更を適用"); self.apply_btn.clicked.connect(self.apply_properties); rl.addWidget(self.apply_btn)
        rl.addWidget(QLabel("グループ操作"))
        for label,fn in [("90°グループ回転",self.rotate_group),("Xミラー",lambda:self.mirror_group("x")),("Zミラー",lambda:self.mirror_group("z")),("X左揃え",lambda:self.align_group("x")),("Z手前揃え",lambda:self.align_group("z")),("中央揃え",lambda:self.align_group("center"))]:
            b=QPushButton(label); b.clicked.connect(fn); rl.addWidget(b)
        rl.addSpacing(8); rl.addWidget(QLabel("重なりルール"))
        self.overlap=QComboBox()
        for k,v in OVERLAP_LABELS.items(): self.overlap.addItem(v,k)
        self.overlap.currentIndexChanged.connect(lambda: self.set_overlap(self.overlap.currentData()))
        rl.addWidget(self.overlap); rl.addStretch()
        splitter.addWidget(left); splitter.addWidget(self.preview); splitter.addWidget(right); splitter.setSizes([300,850,300])
        root.addWidget(splitter,1); self.setCentralWidget(central)
        self.setStatusBar(QStatusBar()); self.statusBar().showMessage("ファイルを追加してください。作業状態は自動保存されます。")
        self.list.itemSelectionChanged.connect(self.preview.update)
        self.xbox.valueChanged.connect(lambda _:None)
        self.rot.currentIndexChanged.connect(lambda _:None)
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks)

    def _spin(self):
        s=QSpinBox(); s.setRange(-10000000,10000000); s.setSingleStep(64); return s

    def apply_theme(self):
        self.setStyleSheet(DARK_QSS if self.theme=="dark" else LIGHT_QSS)
        self.preview.update()

    def toggle_theme(self):
        self.theme="light" if self.theme=="dark" else "dark"; self.apply_theme()

    def change_preview_mode(self):
        self.preview.mode=self.mode_combo.currentData(); self.preview.update()

    def dimensions(self,row):
        src=row["loaded"].size
        return (src[2],src[1],src[0]) if row["rotation"]%180 else src

    def row_by_id(self,uid):
        return next((r for r in self.rows if r["id"]==uid),None)

    def bounds(self):
        if not self.rows: return (0,0,1,1)
        ds=[(r["x"],r["z"],*self.dimensions(r)[::2]) for r in self.rows]
        minx=min(a for a,_,_,_ in ds); minz=min(b for _,b,_,_ in ds)
        return minx,minz,max(a+c for a,_,c,_ in ds)-minx,max(b+d for _,b,_,d in ds)-minz

    def overlaps(self,row):
        sx,sy,sz=self.dimensions(row)
        for other in self.rows:
            if other["id"]==row["id"]: continue
            ox,oy,oz=other["x"],other["y"],other["z"]; ax,ay,az=self.dimensions(other)
            if row["x"]<ox+ax and row["x"]+sx>ox and row["y"]<oy+ay and row["y"]+sy>oy and row["z"]<oz+az and row["z"]+sz>oz: return True
        return False

    def add_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,".mcstructure / ZIPを選択",str(Path.home()),"Structure / ZIP (*.mcstructure *.zip);;All files (*)")
        if not paths:return
        structures=[]
        for path in paths:
            if path.lower().endswith(".mcstructure"): structures.append((path,path))
            elif path.lower().endswith(".zip"):
                try:
                    td=tempfile.TemporaryDirectory(prefix="mcbe_struct_"); self._temp_dirs.append(td)
                    root=Path(td.name).resolve()
                    with zipfile.ZipFile(path) as zf:
                        for m in zf.infolist():
                            rel=PurePosixPath(m.filename)
                            if m.is_dir() or not m.filename.lower().endswith(".mcstructure") or rel.is_absolute() or ".." in rel.parts: continue
                            dst=(root/Path(*rel.parts)).resolve()
                            if os.path.commonpath([str(root),str(dst)])!=str(root): continue
                            dst.parent.mkdir(parents=True,exist_ok=True)
                            with zf.open(m) as src, open(dst,"wb") as out: shutil.copyfileobj(src,out)
                            structures.append((str(dst),f"{Path(path).name} / {m.filename}"))
                except Exception as e: QMessageBox.warning(self,"ZIP読み込み",f"{path}\n{e}")
        added=0
        for path,display in structures:
            try:
                loaded=core.load_structure(path)
                self.rows.append({"id":self._next_id,"path":path,"display_path":display,"loaded":loaded,"x":len(self.rows)*64,"y":0,"z":0,"rotation":0,"mirror_x":False,"mirror_z":False})
                self._next_id+=1; added+=1
            except Exception as e: QMessageBox.warning(self,"読み込みエラー",f"{display}\n{e}")
        self.refresh_list()
        if added:self.changed(f"{added}件の構造物を追加しました")

    def refresh_list(self):
        current=self.selected_id
        self.list.blockSignals(True); self.list.clear()
        for r in self.rows:
            sx,sy,sz=self.dimensions(r)
            label=f"{Path(r['display_path']).name}  •  {sx}×{sy}×{sz}  •  ({r['x']}, {r['y']}, {r['z']})"
            if self.overlaps(r): label="⚠ "+label
            item=QListWidgetItem(label); item.setData(Qt.ItemDataRole.UserRole,r["id"]); self.list.addItem(item)
            if r["id"]==current:item.setSelected(True); self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.preview.invalidate()
        self.update_properties()

    def on_current_item(self,item,previous=None):
        if item:
            self.selected_id=item.data(Qt.ItemDataRole.UserRole)
            self.update_properties()

    def update_properties(self):
        r=self.row_by_id(self.selected_id) if self.selected_id else None
        for w in (self.xbox,self.ybox,self.zbox,self.rot,self.mx,self.mz,self.apply_btn):
            w.setEnabled(r is not None)
        if not r:return
        for w,v in ((self.xbox,r["x"]),(self.ybox,r["y"]),(self.zbox,r["z"])): w.blockSignals(True); w.setValue(v); w.blockSignals(False)
        self.rot.setCurrentIndex(r["rotation"]//90); self.mx.setChecked(r["mirror_x"]); self.mz.setChecked(r["mirror_z"])

    def apply_properties(self):
        r=self.row_by_id(self.selected_id)
        if not r:return
        r.update(x=self.xbox.value(),y=self.ybox.value(),z=self.zbox.value(),rotation=self.rot.currentIndex()*90,mirror_x=self.mx.isChecked(),mirror_z=self.mz.isChecked())
        self.changed("配置を更新しました")

    def selected_rows(self):
        ids={i.data(Qt.ItemDataRole.UserRole) for i in self.list.selectedItems()}
        if not ids and self.selected_id: ids={self.selected_id}
        return [r for r in self.rows if r["id"] in ids]

    def remove_selected(self):
        selected=self.selected_rows()
        if not selected:return
        ids={r["id"] for r in selected}; self.rows=[r for r in self.rows if r["id"] not in ids]
        self.selected_id=None; self.refresh_list(); self.changed(f"{len(ids)}件削除しました")

    def reorder(self,delta):
        selected=self.selected_rows()
        if len(selected)!=1:return
        i=self.rows.index(selected[0]); j=i+delta
        if 0<=j<len(self.rows): self.rows[i],self.rows[j]=self.rows[j],self.rows[i]; self.refresh_list(); self.changed("結合順序を変更しました")

    def repeat_dialog(self):
        r=self.row_by_id(self.selected_id)
        if not r:return
        count,ok=QInputDialog.getInt(self,"連続配置","配置する合計個数（元を含む）",10,1,100)
        if not ok:return
        mode,ok=QInputDialog.getItem(self,"配置方法","配置方法",["X方向","Z方向","グリッド"],0,False)
        if not ok:return
        columns=1
        if mode=="グリッド":
            columns,ok=QInputDialog.getInt(self,"グリッド列数","X方向の列数",10,1,count)
            if not ok:return
        size=self.dimensions(r); key={"X方向":"x","Z方向":"z","グリッド":"grid"}[mode]
        positions=core.calculate_repeat_positions((r["x"],r["y"],r["z"]),size,count,key,columns)
        for pos in positions[1:]:
            clone=copy.copy(r); clone["id"]=self._next_id; self._next_id+=1; clone["x"],clone["y"],clone["z"]=pos
            self.rows.append(clone)
        self.refresh_list(); self.changed(f"{count}個に連続配置しました")

    def rotate_group(self):
        selected=self.selected_rows()
        if not selected:return
        minx=min(r["x"] for r in selected); minz=min(r["z"] for r in selected)
        maxx=max(r["x"]+self.dimensions(r)[0] for r in selected); maxz=max(r["z"]+self.dimensions(r)[2] for r in selected)
        for r in selected:
            oldx,oldz=r["x"],r["z"]; sx,_,sz=self.dimensions(r)
            r["x"]=minx+(maxz-oldz-sz); r["z"]=minz+(oldx-minx)
            r["rotation"]=(r["rotation"]+90)%360
        self.refresh_list(); self.changed("グループを90°回転しました")

    def mirror_group(self,axis):
        selected=self.selected_rows()
        if not selected:return
        lo=min(r["x" if axis=="x" else "z"] for r in selected)
        hi=max(r["x" if axis=="x" else "z"]+self.dimensions(r)[0 if axis=="x" else 2] for r in selected)
        for r in selected:
            size=self.dimensions(r)[0 if axis=="x" else 2]
            r["x" if axis=="x" else "z"]=lo+hi-(r["x" if axis=="x" else "z"])-size
            r["mirror_x" if axis=="x" else "mirror_z"]=not r["mirror_x" if axis=="x" else "mirror_z"]
        self.refresh_list(); self.changed(f"{axis.upper()}ミラーしました")

    def align_group(self,mode):
        selected=self.selected_rows()
        if len(selected)<2:return
        if mode in ("x","center"):
            left=min(r["x"] for r in selected); right=max(r["x"]+self.dimensions(r)[0] for r in selected)
            for r in selected:r["x"]=left if mode=="x" else (left+right-self.dimensions(r)[0])//2
        if mode in ("z","center"):
            front=min(r["z"] for r in selected); back=max(r["z"]+self.dimensions(r)[2] for r in selected)
            for r in selected:r["z"]=front if mode=="z" else (front+back-self.dimensions(r)[2])//2
        self.refresh_list(); self.changed("選択した構造物を整列しました")

    def select_at(self,x,y,mode):
        # In top view, select the topmost structure under the pointer.
        if mode=="top" and self.rows:
            minx,minz,spanx,spanz=self.bounds()
            scale=min((self.preview.width()-80)/max(1,spanx),(self.preview.height()-80)/max(1,spanz))*self.preview.zoom
            ox=(self.preview.width()-spanx*scale)/2-minx*scale+self.preview.pan_x
            oy=(self.preview.height()-spanz*scale)/2-minz*scale+self.preview.pan_y
            wx=(x-ox)/scale; wz=(y-oy)/scale
            for r in reversed(self.rows):
                sx,_,sz=self.dimensions(r)
                if r["x"]<=wx<r["x"]+sx and r["z"]<=wz<r["z"]+sz:
                    self.selected_id=r["id"]; self.refresh_list(); return

    def set_overlap(self,mode):
        if mode:self.overlap_mode=mode; self.changed("重なりルールを変更しました")

    def changed(self,message):
        self.refresh_list(); self.commit_history(); self.save_timer.start(); self.statusBar().showMessage(message,5000)

    def state(self):
        return [{"id":r["id"],"path":r["path"],"display_path":r["display_path"],"x":r["x"],"y":r["y"],"z":r["z"],"rotation":r["rotation"],"mirror_x":r["mirror_x"],"mirror_z":r["mirror_z"]} for r in self.rows]

    def commit_history(self):
        s=(copy.deepcopy(self.state()),self.overlap_mode)
        if self._history and self._history[self._history_index]==s:return
        del self._history[self._history_index+1:]; self._history.append(s)
        if len(self._history)>60:self._history.pop(0)
        self._history_index=len(self._history)-1
        if hasattr(self,"undo_action"):self.undo_action.setEnabled(self._history_index>0); self.redo_action.setEnabled(False)

    def apply_state(self,s):
        records,overlap=s; self.overlap_mode=overlap; self.rows=[]
        cache={}
        for item in records:
            path=item["path"]
            loaded=cache.get(path)
            if loaded is None:loaded=core.load_structure(path); cache[path]=loaded
            self.rows.append({"id":item["id"],"path":path,"display_path":item.get("display_path",path),"loaded":loaded,"x":item["x"],"y":item["y"],"z":item["z"],"rotation":item["rotation"],"mirror_x":item["mirror_x"],"mirror_z":item["mirror_z"]})
        self.selected_id=self.rows[0]["id"] if self.rows else None
        self.overlap.setCurrentIndex(max(0,self.overlap.findData(overlap)))
        self.refresh_list(); self.save_timer.start()

    def undo(self):
        if self._history_index>0:
            self._history_index-=1; self.apply_state(self._history[self._history_index]); self.statusBar().showMessage("元に戻しました")
            self.undo_action.setEnabled(self._history_index>0); self.redo_action.setEnabled(True)

    def redo(self):
        if self._history_index+1<len(self._history):
            self._history_index+=1; self.apply_state(self._history[self._history_index]); self.undo_action.setEnabled(self._history_index>0); self.redo_action.setEnabled(self._history_index+1<len(self._history)-1)

    def cache_source(self,path):
        h=hashlib.sha256(Path(path).read_bytes()).hexdigest(); target=self.sources_dir/f"{h}.mcstructure"
        if not target.exists():
            tmp=target.with_suffix(".tmp"); shutil.copyfile(path,tmp); os.replace(tmp,target)
        return target

    def save_autosave(self):
        try:
            records=[]
            for r in self.rows:
                cached=self.cache_source(r["path"])
                records.append({"instance_id":r["id"],"source_cache":os.path.relpath(cached,self.data_dir),"display_path":r["display_path"],"x":r["x"],"y":r["y"],"z":r["z"],"rotation":r["rotation"],"mirror_x":r["mirror_x"],"mirror_z":r["mirror_z"]})
            payload={"format":"mcstructure-merger-autosave","version":1,"saved_at":__import__("datetime").datetime.now().isoformat(timespec="seconds"),"overlap_mode":self.overlap_mode,"selected_ids":[self.selected_id] if self.selected_id else [],"rows":records}
            tmp=self.autosave_file.with_suffix(".json.tmp")
            with open(tmp,"w",encoding="utf-8") as f:json.dump(payload,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,self.autosave_file)
        except Exception as e:self.statusBar().showMessage(f"自動保存エラー: {e}")

    def restore_autosave(self):
        if not self.autosave_file.is_file():return
        try:
            payload=json.loads(self.autosave_file.read_text(encoding="utf-8"))
            if payload.get("format")!="mcstructure-merger-autosave" or payload.get("version")!=1:return
            cache={}
            for item in payload.get("rows",[]):
                path=str((self.data_dir/item["source_cache"]).resolve())
                if not os.path.isfile(path):continue
                loaded=cache.get(path)
                if loaded is None:loaded=core.load_structure(path); cache[path]=loaded
                self.rows.append({"id":int(item.get("instance_id",self._next_id)),"path":path,"display_path":item.get("display_path",path),"loaded":loaded,"x":int(item["x"]),"y":int(item["y"]),"z":int(item["z"]),"rotation":int(item.get("rotation",0)),"mirror_x":bool(item.get("mirror_x",False)),"mirror_z":bool(item.get("mirror_z",False))})
                self._next_id=max(self._next_id,self.rows[-1]["id"]+1)
            self.overlap_mode=payload.get("overlap_mode","last_defined")
            self.selected_id=self.rows[0]["id"] if self.rows else None
            self.refresh_list()
            if self.rows:self.statusBar().showMessage(f"前回の作業を{len(self.rows)}件復元しました",6000)
        except Exception as e:QMessageBox.warning(self,"自動復元",f"前回の作業を復元できませんでした。\n{e}")

    def merge(self):
        if not self.rows:
            QMessageBox.information(self,"保存","先に構造物を追加してください"); return
        path,_=QFileDialog.getSaveFileName(self,"結合後の.mcstructureを保存","merged_v2.mcstructure","Minecraft structure (*.mcstructure)")
        if not path:return
        if not path.lower().endswith(".mcstructure"):path+=".mcstructure"
        try:
            inputs=[(r["path"],(r["x"],r["y"],r["z"]),r["rotation"],r["mirror_x"],r["mirror_z"]) for r in self.rows]
            result=core.merge_structures(inputs,path,self.overlap_mode)
            QMessageBox.information(self,"結合完了",f"保存しました。\n{result['output']}\n\nサイズ: {result['size'][0]}×{result['size'][1]}×{result['size'][2]}\nパレット: {result['palette_entries']}\nブロック位置データ: {result['block_position_data']}\nエンティティ: {result['entities']}")
            self.statusBar().showMessage("結合とNBT再読み込み検証が完了しました",6000)
        except Exception as e:QMessageBox.critical(self,"結合エラー",str(e))

    def closeEvent(self,event):
        self.save_timer.stop(); self.save_autosave()
        for td in self._temp_dirs:
            try:td.cleanup()
            except Exception:pass
        event.accept()

def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app=QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI",10))
    w=MainWindow(); w.show()
    sys.exit(app.exec())

if __name__=="__main__":
    main()
