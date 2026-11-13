# MCBE Structure Editor

Minecraft Bedrock Edition の `.mcstructure` v2 専用・配置結合ツールです。

## 主な機能

- 複数の `.mcstructure` / ZIP の読み込み
- X/Y/Z 座標指定、90度回転、X/Zミラー
- X方向・Z方向・グリッドで最大100個の連続配置
- Qt6ベースの高DPI対応UI（ダーク／ライト切り替え）
- 3D／上面図切り替えプレビュー。表示面の省略・NumPyによる高速なボクセル抽出・メッシュキャッシュを使用
- 複数選択、グループ移動・回転・整列
- Undo / Redo
- 重なり部分の結合ルール選択
- 作業状態の自動一時保存・次回起動時の復元
- ブロックパレット、2層のブロックデータ、ブロック位置データ、通常エンティティの保持

このアプリは `format_version 2` のみを対象とします。Minecraft本体で使用する前に、元ワールドのバックアップを推奨します。

## Windows版の入手

GitHub の **Actions → Build Windows EXE** から最新の実行結果を開き、Artifacts 内の `MCBE-StructureEditor-windows-x64` をダウンロードしてください。

`v*` タグを作成すると、Windows版 ZIP を GitHub Release にも添付します。

## 起動

1. ZIPを展開します。
2. `MCBE-StructureEditor.exe` を起動します。
3. 初回起動時に Windows Defender SmartScreen が表示される場合があります。配布元とファイルを確認してから実行してください。

Pythonのインストールは不要です。

## 作業の自動復元

編集状態はユーザーのローカルアプリデータフォルダーに一時保存されます。アプリを再起動すると、前回の配置状態を復元します。入力元のファイルはキャッシュされるため、ZIPから読み込んだ構造物も復元できます。

## 開発・ローカル実行

- Python 3.10 以降
- PySide6、NumPy（`requirements-build.txt` に記載）

```powershell
python -m pip install -r requirements-build.txt
python mcstructure_editor_qt.py
```

Windows EXEのビルドは GitHub Actions が Windows runner 上で PyInstaller を実行します。

## 対象外・注意事項

- `.mcstructure` format_version 1 は未対応です。
- 3Dプレビューは近似色による簡易表示です。公式リソースパックの全テクスチャは再現しません。大規模構造物では描画負荷を抑えるため、3Dプレビューの表示ブロック数を制限します。
- NBT検証の成功は、すべてのアドオン・カスタムブロック・Minecraftバージョンとの完全互換性を保証するものではありません。
- 大きな構造物の処理には十分なメモリが必要です。

## License

ライセンスはリポジトリで別途指定されるまで、権利者の許諾なく再配布・改変できるものとして扱わないでください。


## Build status

Windows x64 binaries are built automatically by GitHub Actions on pushes to `main`. Open the Actions tab and download the `MCBE-StructureEditor-windows-x64` artifact from a successful run.
