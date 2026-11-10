# ULTHAR v2 設計書（案）

状態: 草案 r3（Codex 初回レビューの 11 件と再確認の残り 6 件を反映。オーナー確定前）。日付: 2026-11-10。
読み方: §3 の確定事項と §4・§5 の要件 ID、§6・§7 の表が規範部分。§1 は背景、§8〜§10 は根拠と計画。
数値に「提案値」と書いたものはオーナー未確定（§11 で確定する）。

---

## 1. 背景：v1 が守られなかった事実と根本原因

対象: 8 日間・単一セッションの実プロジェクト（2026-11-02〜11-10、transcript 53,947 行、要約 10 回、
AskUserQuestion 368 回、Agent 265 回、SendMessage 767 回、5 系統の並行作業、commander-mode=enforce）。

| 事実 | 数値・内容 |
|---|---|
| スキル本体の呼び出し | 8 日で 1 回。references は各 1〜3 回。必要な参照を指摘後に初めて読んだ例が複数 |
| フックの遮断 | 102 回（tool 44 / spec 27 / brief 12 / git 10 / commander 9）。止めたのは形式だけ |
| フックの形骸化（PM の自認） | brief の必須語を中身なしで埋めて通過、WARN を読み流し、brief gate の遮断を別の担当を立てて回避、STATE.md 未作成 |
| 要件の追跡漏れ | P0 要件が新要件に引き継がれず（追跡の規則が「条件付き参照」で未読） |
| デザインスキル未指示 | UI 作り直しのブリーフにスキル指定なし。「完了」報告後の監査で 24 件 |
| 頼まれていない変更 | 担当が配置・余白値・章を独断で追加し、PM が通過させた |
| 無断の代替 | Codex の上限到達後、Claude レビュアーで代替し「独立レビュー」とだけ報告 |
| 規則の継ぎ足し | プロジェクト内に独自の規則ファイルが 4 本（最大 306 行）。担当へ届く経路はなし |
| 自走しない | 自分だけで進められる作業を止めたまま指示待ち |

根本原因:

- RC1: 規則の配達が PM の記憶と判断に依存。条件付き参照と「再読するな」の節約指示が、長いセッションと要約で規則を蒸発させた。
- RC2: フックは形式（文字列・引用・タグ）しか見ない。PM は「フックを通すこと」を目的化した。
- RC3: オーナーの決定に唯一の置き場がない。会話にあり、要約で消え、独自ファイルが増えた。
- RC4: 受入が PM の自己申告。「テストが通った」が「できた」にすり替わった。
- RC5: 1 つの PM コンテキストに多分野・多系統を詰め込み、PM が中継役になった。
- RC6: 劣化（ツールが使えない）を黙って代替した。
- RC7: v1 自体が継ぎ足しの集積（hooks 8.6k 行、config 168KB）。

---

## 2. 目標と非目標

目標:
- オーケストレータが各プロジェクトの真の窓口として、自プロジェクトの状況と知識を正しく認識・使用し続ける。複数プロジェクトの同時並行でも成り立つ。
- 規則は覚えるものではなく機械が届けるもの。完了は申告ではなく機械が照合するもの。
- 外部ツールは着脱可能。

非目標:
- 「デザインとして良いか」など意味的な品質を機械だけで判定すること（オーナーの静止画判定と評価役で補う）。
- 悪意ある攻撃者に対する完全な隔離（脅威モデルは §4）。
- 同一プロジェクトの複数 PC での運用（D-22）。
- Claude Code 本体（フック API、OTel、transcript 形式、CLI）の抽象化。これは D-03 の対象外の基盤とし、変化は互換層 1 か所で吸収する。

---

## 3. 確定事項（オーナー回答・2026-11-10）

- D-01 v1 を改修せず v2 として再構築する。[owner:v2として再構築]
- D-02 issue の正本は ulthar が持ち、Linear / GitHub Issues / Trello は写しのアダプタとする。[owner:issue正本=ulthar正本＋外部は写し]
- D-03 外部ツールはすべて抽象化し、設定で着脱できる。[owner:ツール置換に備えて抽象化]
- D-04 セッション横断の監視・管理をローカル Web ページで提供する。[owner:モニタ=ローカルWebページ]
- D-05 記憶の中核は薄く自作し、部品は既存を採用する（保存形式は D-20）。[owner:記憶の中核=薄く自作＋部品を採用]
- D-06 セッションはリレー方式（窓口の人格は ulthar 側で持続）。交代は自動＋通知。[owner:セッション=リレー方式][owner:リレー=自動＋通知]
- D-07 通知は Claude のプッシュ通知を主、ntfy を予備とする。[owner:通知=Claudeのプッシュ＋ntfyを予備]
- D-08 普段の発言の指摘は即座に仮の決定とし、一般化と範囲だけをまとめて確認する。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- D-09 定性の受入条件はオーナーが判定する。実装前に静止画で決めて参照画像にする。[owner:定性の判定=オーナー（静止画を先に決める）]
- D-10 プロジェクトの知識は設定可能な knowledge_root に git 管理で置く。[owner:知識の置場=設定できるknowledge_root（git管理）]
- D-11 作業開始前にゴール（受入条件と合格指標）を徹底的に詰めて確定し、原則は定量、定量が難しい場合だけ定性を許す。未達なら自己ループする。監視からゴールの達成状況が見える。[owner:工程を進める・戻すルールと指標]
- D-12 単位の予算上限に達したら止めて通知する。[owner:予算の上限=止めて通知]
- D-13 役割は窓口・担当・評価役・調査役の 4 つに統合する。[owner:役割=4つに統合]
- D-14 作業の種類に応じたスキルは、規則ではなく機械的に注入する。スキルは全文を読み込む。[owner:スキルの機械的注入][owner:スキルは全文を読み込む]
- D-15 工程の仕組みは薄く自作し、Archon の形式を借りる。[owner:工程の仕組み=薄く自作＋Archonの形式]
- D-16 想起の層は自作 FTS5 と claude-mem を実データで測って決める。[owner:想起=両方を測って決める]
- D-17 モニタの画面は小さく自作する。[owner:モニタ画面=小さく自作]
- D-18 記憶は、同一セッション内で忘れて再検索しないと思い出せない状態、別セッションへ引き継げない状態を作らない。全記録の注入によるコンテキスト汚染も避ける。[owner:記憶の管理もultharに]
- D-19 回避の防御は「予防（書き込み時の検査）＋統合時の照合」とし、OS レベルの隔離は行わない。[owner:回避の防御=予防＋統合時の照合]
- D-20 プロジェクト知識の正本は JSONL、SQLite は再生成可能な索引とする。[owner:知識の正本=JSONLが正本、SQLiteは索引]
- D-21 最初の版（R1）は RC1〜RC6 に効く仕組みに絞り、残りは後の版で加える。[owner:最初の範囲=絞る]
- D-22 1 つのプロジェクトは 1 台の PC だけで運用する。knowledge_root の git 管理は履歴とバックアップのためとする。[owner:複数PC=1台のPCのみで運用]

---

## 4. 信頼モデルと強制の境界（TRUST）

脅威モデル: 窓口と担当は「協力的だが、形式を満たして先へ進もうとし、遮断されると別経路を試す」存在。悪意ある攻撃者は対象外（D-19）。
防御は 3 層: (1) 書き込み時の予防、(2) 状態ファイルの保護、(3) 統合時の照合。(1) をすり抜けた変更は (3) で必ず止まることを設計上の保証とする。

- TRUST-01 書き込み可能な全経路を書き込み時に検査する: Edit / Write / MultiEdit / NotebookEdit、Bash と PowerShell のコマンド解析（v1 の tool discipline を移植）、MCP の書き込み系ツール。未知の書き込み可能ツールは既定で遮断（許可リスト方式）。[owner:回避の防御=予防＋統合時の照合]
- TRUST-02 ulthar の状態（`<knowledge_root>/.ulthar/`、`~/.claude/ulthar/`）は ulthar CLI 以外から書けない: フックで直接編集を遮断し、Claude Code の sandbox 設定で Bash からの書き込みも禁止する。[owner:回避の防御=予防＋統合時の照合]
- TRUST-03 ulthar CLI の状態変更コマンドのうち、オーナー由来の記録（確定トークン、決定の確定、定性判定）はフックだけが発行でき、CLI は消費するだけとする（§5.6 GOAL-06）。[owner:回避の防御=予防＋統合時の照合]
- TRUST-04 統合時の照合（内容の来歴）: accepted→integrated の移行の直前に、基準コミットから統合対象までの全変更パスについて、(a) 確定済み契約の対象範囲内、(b) 統合対象でのそのパスの blob ハッシュが、そのパスへの「最後の許可済み書き込みイベント」の事後ハッシュ（TRUST-07）と一致する、(c) そのイベントの agent_id が必要なパックの受領記録を持つ、(d) そのイベントのフェンシングトークンが書き込み時点で有効だった、(e) 最新の証拠が対象ツリーのハッシュに一致、の全てを満たさなければ止める。一致しない変更（記録のない書き込み、記録後の上書き、無効なトークンでの書き込み）は「帰属不明の変更」として一覧にしてオーナーに出す。[owner:回避の防御=予防＋統合時の照合]
- TRUST-05 統合の手段は ulthar CLI のみとし、git の参照更新（merge、update-ref、commit-tree による先送り、push、ブランチの付け替え）の直接実行は統合先ブランチと保護ブランチについて遮断する（v1 git discipline を拡張）。[owner:回避の防御=予防＋統合時の照合]
- TRUST-07 事後ハッシュの記録: Edit / Write / MultiEdit / NotebookEdit の PostToolUse で、書き込まれた各パスの blob ハッシュを書き込みイベントに記録する。ファイルを変更するコマンド（整形、コード生成、テストのスナップショット更新など）は契約に宣言し、`ulthar run <宣言名>` で実行して、変更された全パスの事後ハッシュを記録する。宣言のないコマンドによる変更は (b) で帰属不明になる。[owner:回避の防御=予防＋統合時の照合]
- TRUST-06 遮断のたびに intent_key（対象パスの正規化、またはコマンドの根と第一引数）を記録する。遮断から提案値 30 分以内に、同じセッション木で同じ intent_key が別の道具または別の agent_id で許可された場合を「回避の疑い」として記録し、オーナーに通知する。[owner:状態監視・管理]

---

## 5. 要件

### 5.1 中核（CORE）
- CORE-01 ulthar の機能は単一の CLI `ulthar` から提供し、窓口・担当・フックはこれを通して状態を読み書きする。[owner:v2として再構築]
- CORE-02 フックは単一のディスパッチャから起動し、`updatedInput` を返すのはディスパッチャ 1 か所だけとする。[spec:docs/plans/v2/design.md:§8 P-05]
- CORE-03 実行時の状態（セッション、イベント、リース、使用量、遮断、質問、確定トークン）は `~/.claude/ulthar/runtime.db`（SQLite WAL）に置き、同期しない。[owner:知識の置場=設定できるknowledge_root（git管理）]
- CORE-04 プロジェクトの知識は `<knowledge_root>/.ulthar/` の JSONL を正本とする。各レコードは project_id、ハッシュ ID、seq、書き手（session_id / agent_id / hook）を持つ。書き込みは ulthar CLI がプロジェクト単位のファイルロックの下で行い、seq はロック内で「既存の最大値＋1」を採番する（書き手は 1 台の PC の ulthar だけなので、seq は全順序になる）。同じロック内で索引（index.db）を更新する。索引は JSONL から再生成可能。[owner:知識の正本=JSONLが正本、SQLiteは索引][owner:複数PC=1台のPCのみで運用]
- CORE-05 knowledge_root はプロジェクトごとに設定でき、製品リポジトリの外（例: 計画用リポジトリ）を指せる。JSONL の git コミットは ulthar CLI が区切り（単位の移行、リレー）ごとに行う（push はオーナーの許可で行う）。knowledge_root には運用中の PC の識別子を記録し、別の PC の ulthar はそのプロジェクトへの書き込みを拒否する。[owner:知識の置場=設定できるknowledge_root（git管理）][owner:複数PC=1台のPCのみで運用]
- CORE-06 v1 の安全系ガード（git discipline、tool discipline の削除・シェル規則、commander の書き込み禁止、grant ファイル）は機能を落とさず移植する。[owner:v2として再構築]
- CORE-07 transcript の取り込み: すべてのフック呼び出しの冒頭で、当該セッションの transcript を前回の位置から読み、§6 の対象イベント種別を runtime.db と Episode に取り込む。位置は (session_id, ファイルサイズ, 行番号) で記録し、SessionStart では未取り込みの全区間を取り込む。区間の欠落（ファイルの縮小・行の不連続）は検出してモニタに出す。[owner:記憶の管理もultharに]

### 5.2 ポートとアダプタ（PORT）
- PORT-01 外部依存はポート Tracker / Reviewer / OwnerChannel / Notifier / DocLint / Embedder / MemoryBackend を通す。[owner:ツール置換に備えて抽象化]
- PORT-02 ポートごとに契約テストを 1 本持ち、各アダプタはそれに合格することを着脱の条件とする。[owner:ツール置換に備えて抽象化]
- PORT-03 外部ツールへの直接の書き込み（MCP の書き込み系ツール、`gh issue|pr` の書き込み、既知のベンダー API への HTTP 書き込み）は、選択中のアダプタに関係なく遮断する。外部への書き込みは ulthar のアダプタ処理だけが行う。[owner:ツール置換に備えて抽象化]
- PORT-04 アダプタの選択は設定 1 行で切り替える。[owner:ツール置換に備えて抽象化]
- PORT-05 能力登録: 各能力（Codex、Claude プッシュ、ntfy、各スキル）について健全性検査（実際の呼び出しの成否）と残量を記録する。直近の検査が失敗した能力は「利用不可」とし、その代替は OwnerChannel での承認記録がなければ受入に使えない。[owner:v2として再構築]
- PORT-06 写しの同期は冪等キー付きで行い、外部側で ulthar を経ずに変更された項目（ずれ）を検出してオーナーに出す。[owner:issue正本=ulthar正本＋外部は写し]

### 5.3 issue（ISS）
- ISS-01 issue の正本は knowledge_root の JSONL。状態、優先度、ラベル、関連、コメント、契約・決定・証拠へのリンクを持つ。[owner:issue正本=ulthar正本＋外部は写し]
- ISS-02 Tracker アダプタは写しとして同期する。[owner:issue正本=ulthar正本＋外部は写し]
- ISS-03 既存 Linear issue を native へ取り込むインポータを持つ。[owner:issue正本=ulthar正本＋外部は写し]

### 5.4 記憶（MEM）
- MEM-01 記憶は型を持つ: Decision（規範）、Fact（事実）、Lesson（手順の教訓）、Episode（出来事）、Term（用語）。型を混ぜない。[owner:記憶の管理もultharに]
- MEM-02 Decision の起点はオーナーの発言・回答だけ。担当が書けるのは Fact と Episode だけ。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- MEM-03 Fact は出典 `path:line@blobhash` を必須とし、出典ファイルの blob ハッシュが変わったら stale に落とす（取り込み時と注入時に照合）。[owner:記憶の管理もultharに]
- MEM-04 記憶は層を持つ: L0 中核（SessionStart と要約後）、L1 作業集合（担当の起動時と再開時）、L2 想起（自動では見出しと ID のみ、本文は `ulthar recall/get` で取得）、L3 原本（ポインタのみ）。[owner:記憶の管理もultharに]
- MEM-05 L0 と L1 の選択は規則（project_id、タグ、パス、単位）による決定的な選択とし、類似度で選ばない。[owner:記憶の管理もultharに]
- MEM-06 注入の総量予算: 1 回の注入は 10,000 字以内。担当の起動時の総量（スキル全文＋L1）と窓口の総量（L0＋L2 見出し）にそれぞれ上限（提案値: 担当 60,000 トークン、窓口 L0 6,000 字）を持つ。優先順位は 拘束力のある決定 ＞ 契約 ＞ スキル ＞ 用語 ＞ 事実 ＞ Episode。拘束力のある決定と契約は切り詰めない。上限を超える場合は切り詰めずに遮断し、単位の分割またはパックの見直しを求める（fail closed）。それ以外は優先順位の低い順に、ID と見出しのポインタへ置き換える。[owner:記憶の管理もultharに]
- MEM-07 要約: PreCompact で要約方針（オーナー発言・決定・未解決事項を保持）を指示する。要約本文は transcript に記録されるため、CORE-07 の取り込みで Episode として保存する（モデルの行動に依存しない）。[owner:記憶の管理もultharに]
- MEM-08 オーナーの発言、AskUserQuestion の質問と回答、担当の完了報告、要約本文は全文を Episode として保存する。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- MEM-09 記憶はプロジェクト範囲と全体範囲を持つ。全体範囲に入るのはオーナーが範囲を「全体」と確認したものだけ。他プロジェクトの記憶は見出し検索のみ許し、自動注入しない。[owner:記憶の管理もultharに]
- MEM-10 記録前に秘密情報の検査を通す。外部由来（Web、外部 issue）の内容は「データ」印を付け、命令として注入しない。[owner:記憶の管理もultharに]
- MEM-11 記憶は置き換え（supersede）で更新し、削除はオーナーの確認を経た場合だけ行う。[owner:記憶の管理もultharに]
- MEM-12 L2 の検索バックエンドは MemoryBackend ポートの下に置く（R1 は自作 FTS5 trigram）。[owner:想起=両方を測って決める]
- MEM-13 プロジェクトの CLAUDE.md は ulthar から生成する一方向の写しとし、ulthar 利用プロジェクトでは Claude Code の自動メモリを無効にする。[owner:記憶の管理もultharに]

### 5.5 決定の取り込み（DEC）
- DEC-01 UserPromptSubmit でオーナーの発言を保存し、設定したモデル（既定 Haiku）で指摘・禁止・好み・用語の候補を抽出する。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- DEC-02 候補は直ちに仮の決定として L1・L0 の対象に入れる。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- DEC-03 送り出しの障壁: Agent と SendMessage の PreToolUse で、直前までのオーナー発言の抽出が完了していることを確かめる。未完了または抽出が失敗した発言は、原文のまま「オーナーの未整理の発言」として注入する（取りこぼさない側に倒す）。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- DEC-04 確認するのは正規化文、適用範囲（プロジェクト/全体、タグ、パス）、置き換える旧決定だけ。単位の移行時またはリレー前に、未確認の候補をまとめて 1 回の質問で出す。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- DEC-05 既存決定との重複・矛盾を書き込み時に検査し、矛盾は質問にする。[owner:決定の取込=即仮決定＋一般化だけまとめて確認]
- DEC-06 指摘型の決定は、同じ分類の範囲に限った洗い出しの単位を自動で起票する。別分類への拡大は行わない。[owner:工程を進める・戻すルールと指標]
- DEC-07 確定済み決定への再違反（オーナーの差し戻しが既存決定 ID に紐付いたもの）を「再発」として記録する。[owner:工程を進める・戻すルールと指標]

### 5.6 ゴール契約と状態機械（GOAL）
- GOAL-01 単位ごとにゴール契約を作る。要素: 意図（オーナーの原文と解釈）、やらないこと（必須）、対象範囲（パスの glob）、受入条件 AC-n、予算。[owner:工程を進める・戻すルールと指標]
- GOAL-02 各 AC は条件文（EARS 形式推奨）、種別（定量/定性）、検証（契約に書かれた実行コマンド）、閾値（両端と包含を明記）、証拠の形、出典タグを持つ。[owner:工程を進める・戻すルールと指標]
- GOAL-03 定性の AC は理由、採点基準（良い例・悪い例つきの段階）を必須とし、判定者はオーナー。[owner:定性の判定=オーナー（静止画を先に決める）]
- GOAL-04 契約確定前の深掘り: 解釈の対案を最低 2 つ提示して選ばせる、境界と反例を質問する、評価役が「質問なしで全 AC を検証できるか」「要求の名詞が全て AC にあるか」を審査し、結果を記録する。[owner:工程を進める・戻すルールと指標]
- GOAL-05 契約 lint を機械で実行する: 曖昧語辞書（日英）、数値の両端、検証コマンドの有無と実行可能性（乾式実行）、出典の有無、やらないことの有無、定性 AC の比率上限（提案値 30%）。[owner:工程を進める・戻すルールと指標]
- GOAL-06 確定トークン: 契約の全文とハッシュを AskUserQuestion で出し、PostToolUse フックがその回答を読み、(project_id, unit_id, contract_sha, 質問イベント ID, 回答イベント ID) に束縛した一回限りのトークンを runtime.db に発行する。contract_confirmed への移行はこのトークンを原子的に消費した時だけ成立する。トークンはフック以外から発行できない。[owner:工程を進める・戻すルールと指標]
- GOAL-07 単位の工程は §7 の状態機械で表し、移行は `ulthar unit advance` からのみ行い、§7 の前提条件を機械が検査する。[owner:工程を進める・戻すルールと指標]
- GOAL-08 製品ファイルへの書き込み（TRUST-01 の全経路）は、書き込み先が contract_confirmed 以降の単位の対象範囲内である時だけ許可する。[owner:工程を進める・戻すルールと指標]
- GOAL-09 build は担当の自己ループ。担当は `ulthar verify --self` で検証を回し、定量 AC が全て合格するまで繰り返す。停滞（同一 AC の測定値が提案値 2 回連続で改善しない）または予算上限で halted に移し、原因（実装/設計/要求/評価/環境）を分類して記録する。[owner:工程を進める・戻すルールと指標]
- GOAL-10 信頼できる検証実行: `ulthar verify` は契約に書かれた検証コマンドだけを実行し（担当は選べない）、コマンド、stdout/stderr のハッシュ、ツールの版、終了コード、成果物のハッシュ、作業ツリーの状態（変更・未追跡を含む）、ツリーハッシュ、基準コミット、契約 sha、実行者 agent_id を証拠マニフェストに記録する。担当が書いた証拠ファイルは受け付けない。[owner:工程を進める・戻すルールと指標]
- GOAL-11 verify 工程では、担当と別の agent_id の評価役が `ulthar verify` を実行し、ブロッカーの有無を記録する。評価役の agent_id が担当と同じ場合は移行を拒否する。[owner:工程を進める・戻すルールと指標]
- GOAL-12 戻りの移行と「すり抜け」の記録は §7 のとおり。受入後のオーナー差し戻しは、捉えるべきだった AC の有無とともに記録し、AC がなかった場合は lint 規則の追加候補にする。[owner:工程を進める・戻すルールと指標]
- GOAL-13 工程の定義は YAML（Archon 形式を参考）で記述し、コードに埋め込まない。[owner:工程の仕組み=薄く自作＋Archonの形式]

### 5.7 品質ゲート（QG）
- QG-01 判定しにくい分野でも可能な限り定量 AC にする（デザイン lint、ビューポート行列での重なり・はみ出し・振動の測定、axe、操作スクリプト、参照画像との差分）。[owner:工程を進める・戻すルールと指標]
- QG-02 見た目を伴う単位は、実装前に静止画の案を出してオーナーが選び、選ばれた静止画を参照画像として差分 AC に使う。[owner:定性の判定=オーナー（静止画を先に決める）]
- QG-03 評価役は、ユーザーに見える変更のうち AC に結び付かないものを列挙し、オーナーへの質問に回す。[owner:工程を進める・戻すルールと指標]
- QG-04 定性 AC は owner_review 工程でスクリーンショット付きでまとめて判定してもらう。判定の記録は確定トークンと同じ方式でフックが発行する。[owner:定性の判定=オーナー（静止画を先に決める）]

### 5.8 スキルパック（SKL）
- SKL-01 スキルパックを設定で定義する: 一致条件（パスの glob）、スキル一覧、既定 AC、評価役チェックリスト。[owner:スキルの機械的注入]
- SKL-02 作業の種類は契約の対象範囲から機械的に決める。宣言と推定が食い違えば契約 lint で止める。[owner:スキルの機械的注入]
- SKL-03 パックごとの担当定義（`ulthar-creator@<pack>` 等）は install 時に生成し、frontmatter の `skills:` に全スキルを列挙する。送り出しの PreToolUse は、単位のパックに合う担当型でなければ `updatedInput` で書き換え、書き換えられなければ遮断する。[owner:スキルは全文を読み込む]
- SKL-04 受領記録: SubagentStart で agent_type が生成済みのパック担当であること、その定義ファイルのハッシュと列挙スキルの SKILL.md ハッシュが登録値と一致することを照合し、(agent_id, pack, ハッシュ群) を受領記録として保存する。一致しなければその担当の書き込みを全て遮断する。[owner:スキルの機械的注入]
- SKL-05 担当の書き込み（TRUST-01 の全経路）は、書き込み先パスに必要なパックの受領記録をその agent_id が持つ時だけ許可する。[owner:スキルの機械的注入]
- SKL-06 パックの既定 AC は契約に自動で入り、評価役にも同じパックの担当定義を使う。[owner:スキルの機械的注入]

### 5.9 セッション・リレー・リース（SES）
- SES-01 窓口の人格はプロジェクトに属し、SessionStart と要約後に L0（憲章、拘束力の強い決定、前回からの Episode 差分の見出し、回答待ち、キュー、現在の単位）を注入する。[owner:セッション=リレー方式]
- SES-02 リース: プロジェクトごとに窓口のリースを 1 つ持つ。取得・更新・移譲は runtime.db のトランザクションで行い、単調増加のフェンシングトークンを発行する。ulthar の状態変更と製品ファイルへの書き込みは、そのセッションが現在のトークンを保持する時だけ許可し、書き込みイベントには許可時のトークンを記録する（TRUST-04 (d) で照合）。リースは 1 台の PC の runtime.db で管理する（D-22）。リースの更新は各フック呼び出し時に行い、提案値 15 分更新がなければ失効する。[owner:セッション=リレー方式]
- SES-03 リレーの契機は (a) 単位が done または halted に移った時、(b) 窓口のコンテキスト使用率が閾値（提案値 60%）を超えた時。使用率は CORE-07 で取り込んだ最新の API 使用量（入力＋キャッシュ作成＋キャッシュ読み込み ÷ 窓口のコンテキスト長）で算出する。[owner:リレー=自動＋通知]
- SES-04 契機に達したら、PreToolUse は `ulthar relay` と読み取り系以外の道具を遮断する（自動化の強制点）。`ulthar relay` は引き継ぎ条件（未記録の単位状態がない、未発行の質問がない、動作中の担当が全て停止または登録済み）を検査し、Episode を書き、`claude --bg` で新しい窓口を起動し、リースを移譲し、通知する。旧セッションは移譲後に書き込みが全て遮断される。[owner:リレー=自動＋通知]
- SES-05 障害回復: リースが失効したプロジェクトでは、次に起動した窓口が SessionStart でリースを取得し、CORE-07 の取り込みで未取り込み区間を回収してから作業を始める。[owner:セッション=リレー方式]
- SES-06 担当は単位ごとに新しく起動し、同一担当の再開は同一単位の修正に限る。[owner:セッション=リレー方式]

### 5.10 自走と通知（AUT）
- AUT-01 自走キュー: オーナー不在時に窓口だけで進められる単位の列を持つ。窓口が停止（Stop）した時にキューが残っていれば、Stop フックで継続させ（連続 8 回まで）、上限に達したらリレーで新しい窓口に引き継ぐ。[owner:v2として再構築]
- AUT-02 質問はキューに入れ、単位の移行時またはリレー前にまとめて出す。作業を止める質問だけ即時に出す。回答待ちの間は別の単位を進める。[owner:v2として再構築]
- AUT-03 通知: 送信は Claude プッシュを試み、健全性検査（PORT-05）が失敗している時、または送信が失敗した時は ntfy に切り替える。フックとモニタからの通知は ntfy を使う。全通知は送信結果を記録する。重要通知（回答待ち、halted、回避の疑い、能力の劣化）は、そのプロジェクトに対するオーナーの操作（オーナーの発言、AskUserQuestion への回答、モニタでの操作）が記録されるまで、提案値 30 分ごとに最大 3 回再送する。オーナーの操作の記録をもって既読とみなす（配信成功は既読としない）。[owner:通知=Claudeのプッシュ＋ntfyを予備]

### 5.11 モニタ（MON）
- MON-01 `ulthar monitor` は 127.0.0.1 のみで待ち受け、トークン付きのローカル Web ページを提供する。標準ライブラリで実装する。[owner:モニタ画面=小さく自作]
- MON-02 R1 の表示: セッション一覧（プロジェクト、単位、工程、最終活動）、コンテキスト使用率と要約回数、ガード別の遮断数と率、回避の疑い、回答待ち、能力の状態、単位ごとの AC の合否と測定値。[owner:状態監視・管理][owner:工程を進める・戻すルールと指標]
- MON-03 管理操作は ulthar の状態への書き込みに限る: 凍結/再開、リレー要求、キュー並べ替え。コマンド実行・push・デプロイ・ファイル編集はできない。[owner:状態監視・管理]
- MON-04 データ源は runtime.db（フックのイベント、transcript 取り込み）と `claude agents --json`。[owner:状態監視・管理]

### 5.12 予算（BUD）
- BUD-01 単位ごとにトークン上限とループ回数上限を契約に持つ。80% で通知、上限で halted に移して通知する。予算を理由に合格にしない。[owner:予算の上限=止めて通知]

### 5.13 役割とモデル（ROLE）
- ROLE-01 役割は窓口・担当・評価役・調査役の 4 つ。contract-author と auditor は評価役に、committer は窓口の CLI に吸収する。[owner:役割=4つに統合]
- ROLE-02 評価役の既定は Codex。利用不可時の代替は PORT-05 に従う。[owner:通知=Claudeのプッシュ＋ntfyを予備]

### 5.14 移行（MIG）
- MIG-01 既存プロジェクトの規則ファイル（オーナー指摘の記録、デザイン規則、長期方針、追跡表）を Decision / Term / Lesson の候補として取り込み、分類ごとにまとめて確認する。[owner:記憶の管理もultharに]
- MIG-02 v1 の条件付き references、STATE / LANES / TOML ブリーフ、standard / strict の二重モード、形式検査の brief gate は v2 移行完了後に削除する。[owner:v2として再構築]

### 5.15 自己評価（EVAL）
- EVAL-01 実プロジェクトの違反事例を再現データ（フックへの入力と transcript の断片）にし、各段階の受入テストとする（§10）。[owner:v2として再構築]

---

## 6. データモデル

### 6.1 knowledge_root/.ulthar/（git、JSONL 正本、共通フィールド: id, seq, project_id, writer, ts, supersedes）

| ファイル | 固有フィールド |
|---|---|
| decisions.jsonl | status(provisional/confirmed/superseded/rejected), text, raw_episode_id, scope(project/global), tags[], paths[], binding(bool), confirm_token_id |
| facts.jsonl | text, src_path, src_line, src_blob, status(valid/stale), valid_from, invalid_at |
| lessons.jsonl | when, then, evidence_ids[], scope |
| terms.jsonl | term, preferred, forbidden[], decision_id |
| episodes.jsonl | session_id, kind(owner_msg/ask/answer/compact_summary/agent_report/relay/owner_pushback), unit_id, text, transcript_ref |
| issues.jsonl | title, status, priority, labels[], links[], mirrors{adapter: {ref, idempotency_key, last_sync, drift}} |
| units.jsonl | unit_id, issue_id, state, contract_ref, history[{from, to, actor, ts, reason, evidence_ref}] |
| contracts/<unit>/v<n>.yaml | intent{raw, interpretation}, non_goals[], scope_globs[], packs[], acs[{id, statement, kind, verify_cmd, threshold{lo, hi, lo_incl, hi_incl}, evidence_kind, rubric?, sources[]}], budget{tokens, loops}, sha |

### 6.2 runtime.db（ローカル、SQLite WAL）

| 表 | 主な列 |
|---|---|
| sessions | session_id, project_id, role, agent_id, started, last_seen, transcript_path, offset, ctx_used, ctx_window, compactions |
| leases | project_id, host_id, session_id, fence_token, renewed_at, expires_at |
| events | ts, session_id, agent_id, agent_type, hook, tool, decision(allow/deny/rewrite), guard_id, intent_key, fence_token, paths[], post_hashes{path: blob}, detail |
| tokens | token_id, kind(contract_confirm/decision_confirm/qual_judgement/substitution_approval), bound{...}, issued_by_hook, consumed_at |
| receipts | agent_id, pack, agent_def_hash, skill_hashes, ts |
| evidence | unit_id, manifest_path, tree_hash, contract_sha, runner_agent_id, result |
| questions | qid, project_id, unit_id, status(queued/asked/answered), payload |
| capabilities | name, healthy, last_check, remaining, detail |
| notifications | nid, project_id, channel, kind, sent_ok, acked_by_event_id, retries |
| suspicions | block_event_id, allow_event_id, intent_key, notified |

---

## 7. 状態機械（単位）

| 現状態 | 次状態 | 実行者 | 前提条件（機械が検査） | 原子的に書くもの | 失敗時 |
|---|---|---|---|---|---|
| intake | contract_draft | 窓口 | issue が存在 | units.history | — |
| contract_draft | contract_confirmed | 窓口（CLI） | lint 合格、評価役の事前審査記録あり、未消費の contract_confirm トークンが sha と一致 | トークン消費、units.history | contract_draft のまま |
| contract_confirmed | build | 窓口 | パス glob からパック決定済み、予算設定済み | units.history | — |
| build | quant_verified | 担当→窓口 | 最新の `ulthar verify --self` で全定量 AC 合格 | 証拠、units.history | build（ループ） |
| quant_verified | owner_review | 窓口 | 評価役（別 agent_id）の `ulthar verify` で全定量 AC 合格、ブロッカー 0、証拠のツリーハッシュが現在と一致、AC に結び付かない見える変更の一覧が空またはオーナー判断済み | 証拠、units.history | build（不合格）／contract_draft（AC 不能・矛盾） |
| owner_review | accepted | 窓口（CLI） | 定性 AC が全て qual_judgement トークンで合格（定性 AC がなければ即時） | トークン消費、units.history | build（不合格の定性 AC を差し戻し） |
| accepted | integrated | 窓口（CLI） | TRUST-04 の照合に合格、リース保持 | 統合コミット、units.history | accepted のまま（帰属不明の変更をオーナーへ） |
| integrated | done | 窓口 | 統合先で検証の再実行に合格 | units.history | accepted に戻し差分を記録 |
| done | reopen | オーナー由来 | オーナーの差し戻し（Episode owner_pushback） | すり抜け記録（DEC-07、GOAL-12） | — |
| reopen | contract_draft | 窓口 | — | units.history | — |
| 任意 | halted | 自動 | 予算上限／停滞／凍結 | 理由、通知 | — |
| halted | 直前の状態 | 窓口 | 凍結解除、または予算の再設定（オーナー）、または再計画（新しい契約版） | units.history | — |

共通規則: すべての移行は、units の状態（units.jsonl の新レコード）と history の追記、表の「原子的に書くもの」を、CORE-04 の同じロック内で 1 つの操作として書く。いずれかが失敗した場合は何も書かない。

統合先ブランチは設定値（既定 develop、U-08 で確定）。

---

## 8. 検証済みの前提（2026-11-10、Claude Code 2.1.322、Windows）

| ID | 前提 | 結果 |
|---|---|---|
| P-01 | PreToolUse の `updatedInput` で Agent の prompt を書き換える | 成立 |
| P-02 | SubagentStart の `additionalContext` で担当に注入する | 成立。SendMessage での再開時にも再発火 |
| P-03 | 担当内の PreToolUse に `agent_id` / `agent_type` が付く | 成立 |
| P-04 | `claude --bg` で新セッションを起動し、`claude agents --json` で対話中を含め列挙 | 成立 |
| P-05 | 複数フックの `updatedInput` は合成されない（二次情報） | 未検証。CORE-02 で単一化して回避 |
| P-06 | サブエージェントの `skills:` は全文を起動時に読み込む | 公式文書で確認。R1 の最初の作業で実機検証し、不成立なら SKL-03 を「SubagentStart で全文を注入」に切り替える（総量は MEM-06 に従う） |
| P-07 | 注入の上限は 1 回 10,000 字 | 公式文書で確認 |
| P-08 | Stop フックによる継続は連続 8 回まで | 公式文書で確認 |
| P-09 | フックからの Claude プッシュ通知 | 文書なし。AUT-03 で ntfy に切り替える |
| P-10 | statusLine の `context_window` が Windows で正しいか | 判定不能。SES-03 は transcript の使用量を使う |
| P-11 | コンテキスト閾値のイベント | 存在しない。SES-03/04 で代替 |
| P-12 | 要約本文が transcript に記録される | 成立（実 transcript に `isCompactSummary` 行が要約回数と同数ある） |
| P-13 | install 時に生成した担当定義が、起動中のセッションで使えるか | 未検証。R1 の最初の作業で検証する |
| P-14 | Claude Code sandbox で Bash の書き込み先を制限できるか（Windows） | 未検証。R1 で検証し、不成立なら TRUST-02 はフック遮断のみとして明記 |

---

## 9. 採用・借用・自作

| 区分 | 対象 |
|---|---|
| 採用 | Claude Code のフック・サブエージェント `skills:`・SubagentStart/SessionStart(compact) 注入・`claude --bg`/`agents --json`・sandbox、Codex、Claude プッシュ通知、ntfy |
| 借用（設計・形式） | Archon（工程 YAML、until/max_iterations、承認ゲート）、Gas Town（持続する人格と使い捨てセッション、検証付きマージ列）、Anthropic の sprint contract、tigerless agent-memory（型別スキーマ、supersede、削除は提案のみ）、context-mode（trigram、要約後の再開ブロック）、Memanto/Graphiti（型一覧、二時刻の有効期間） |
| 測って決める（R2 以降） | L2 想起: 自作 FTS5 trigram と claude-mem |
| 自作 | 確定トークン、決定台帳、ゴール契約と状態機械、信頼できる検証実行、統合時の照合、スキルパックと受領記録、リースとリレー、transcript 取り込み、回避検出、モニタ、issue 正本とアダプタ |

---

## 10. 版と段階

R1（D-21: RC1〜RC6 に効くものだけ）:

| 段階 | 内容 | 受入（再現データ） |
|---|---|---|
| R1-a 基盤 | runtime.db、CLI 骨格、単一ディスパッチャ、transcript 取り込み、v1 安全系ガードの移植、P-06/P-13/P-14 の実機検証 | 実 transcript の再生で、全オーナー発言と要約本文 10 件が Episode になり、欠落 0 |
| R1-b 記憶と決定 | Decision/Fact/Term/Episode、L0/L1 注入と総量予算、決定の取り込みと送り出しの障壁、まとめての確認 | 「担当にデザインの決定が届かない」事例で、送り出し時に該当決定が注入される／未抽出の発言が原文で注入される／予算超過で拘束的決定が切り詰められず遮断される |
| R1-c 契約と工程 | ゴール契約、lint、確定トークン、状態機械、信頼できる検証実行、GOAL-08 の書き込み制限 | 確定トークンなしの移行が拒否される／曖昧語の契約が止まる／担当が書いた証拠が受け付けられない／確定前の製品書き込みが止まる |
| R1-d パックと統合 | スキルパック、受領記録、書き込み時照合、統合時の照合と git 参照更新の遮断 | 「no-ui の自己申告」「スキル指定なし」の事例で UI パスへの書き込みが止まる／Bash 経由の書き込み、および正規の Edit の後の宣言のない上書きが、統合時に帰属不明として止まる／失効したフェンシングトークンでの書き込みが統合時に止まる |
| R1-e 分離と劣化 | リースとフェンシング、リレー（契機・強制・回復）、能力登録と代替承認、PORT-03 の遮断、通知の切り替え、最小モニタ（MON-02/03） | 2 つ目の窓口の書き込みが止まる／閾値超過でリレー以外が遮断される／Codex 利用不可時に承認なしの代替が受入に使えない／Linear MCP の直接書き込みが止まる／遮断後の別担当での許可が回避の疑いとして通知される |

R2 以降: L2 想起の比較（FTS5 と claude-mem）、整理ジョブ、教訓の層と全体への昇格、Tracker の写し（linear/github/trello/beads）、モニタの管理操作の拡充、予算の日次・週次、EVAL の週次集計、移行インポータ（MIG）、v1 旧機構の削除。

---

## 11. 未決事項（オーナー確定が必要）

- U-01 注入の総量予算（提案値: 担当の起動時 60,000 トークン、窓口 L0 6,000 字）。
- U-02 リレーのコンテキスト閾値（提案値: 60%、範囲 50–70%）とリースの失効時間（提案値: 15 分）。
- U-03 停滞判定（提案値: 同一 AC が 2 回連続で改善なし）と単位の既定ループ上限（提案値: 5 回）。
- U-04 定性 AC の比率上限（提案値: 30%）。
- U-05 曖昧語辞書の初期語彙（日英）。
- U-06 knowledge_root を持たないプロジェクトの既定の置き場。
- U-07 単位の既定予算。
- U-08 v2 の開発リポジトリと統合先ブランチ（本リポジトリに develop を作るか、別の開発リポジトリか）。
- U-09 回避の疑いの時間窓（提案値: 30 分）、重要通知の再送間隔（提案値: 30 分）と最大再送回数（提案値: 3 回）。
