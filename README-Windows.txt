MCBE Structure Editor - Windows版

起動方法
1. このZIPを任意のフォルダーへ展開してください。
2. MCBE-StructureEditor.exe をダブルクリックします。
3. Pythonの別途インストールは不要です。

主な機能
- Bedrock .mcstructure format_version 2 の読み込み・結合
- ZIP内のmcstructureの読み込み
- 座標指定、回転、X/Zミラー、最大100個の連続配置
- 重なり処理ルール、グループ操作、Undo/Redo
- Qt6ベースの高DPI対応UI（ダーク／ライト切り替え）
- 上面図／3D近似ボクセルプレビューの切り替え
- 上面図：選択済み構造物を左ドラッグで移動（離すと64ブロック単位にスナップ）
- 3D：左ドラッグで視点回転、Shift＋左ドラッグで選択構造物を移動
- 両プレビュー共通：右ドラッグで画面移動、ホイールでズーム
- ツールバーから選択物を90°回転／逆90°回転
- Undo / Redo：Ctrl+Z / Ctrl+Y
- 作業状態の自動一時保存・次回起動時の復元

自動保存先
%LOCALAPPDATA%\McStructureMerger

注意
- format_version 1 は未対応です。
- 3Dプレビューは近似色による表示で、公式リソースパックの全テクスチャを再現しません。
- 非常に大きな構造物では、3D表示の負荷を抑えるため描画ブロック数を制限します。
- Minecraft本体で使う前にワールドをバックアップしてください。
- Windows SmartScreenが表示された場合は、ファイルの入手元と内容を確認してください。

問題が起きた場合
GitHubリポジトリの Issues に、発生した操作・エラー文・Windowsのバージョンを記載してください。
