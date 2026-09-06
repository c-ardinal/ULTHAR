# Discipline hooks — masters

## 名称: ULTHAR（命名根拠, 2026-09-05）

本スキルは **ULTHAR**（Unified Lead, Task Handoff & Acceptance Review）と呼ぶ。
クトゥルー神話由来の語をバックロニムにする、という命名方針に従った名称。

Ulthar はラヴクラフトの Dreamlands に登場する町で、「何人も猫を殺してはならない」という
掟が絶対不可侵とされる場所である。命名の要点はここにある。この skill の規律はエージェント
が従うよう頼まれる散文ではなく、PreToolUse/Stop フックによって機械的に強制される
——**司令塔自身の違反も含めて**。実際、この skill 自体の開発中もコーディネーターは
`brief-gate-guard` と `tool-discipline-guard` に複数回ブロックされた。この経緯を記録するのは、
根拠が記録された名前だけが生き残るためである。

## アーキテクチャ（2026-09-05〜、Python ランタイム）

PowerShell 版（`dispatch.ps1` + `dispatch-map.json` + ガードごとの `.ps1`）は
2026-09-05 に退役・削除済み。ロールバック用の `.ps1` マスター一式は**残置していない**
——本配布物に `.ps1` は一切含まれない。ロールバックが必要な場合は git 履歴から
PowerShell 版のコミットを復元すること。
現行は **Python 単一ランタイム**:

- `settings.json` には **イベント種別ごとに `dispatch.py` 1 本だけ**を登録する
  （PreToolUse / Stop / SessionStart 各 1 エントリ）。`settings.json` に
  本スキルが配置していないサードパーティのフックエントリが同居していても、それは
  対象外——`install.py` は自分がコピーしたものだけを扱い、他は読み書きしない。
  `PostCompact` は `config.json` の `dispatch.events` に登録されておらず、
  `settings.json` にも本スキルはエントリを配置しない（下記参照）。
- ルーティングは `dispatch-map.json` ではなく **`config.json` の `dispatch.events`**
  が持つ。フックを増やしても `settings.json` は不変。`dispatch.events` に無い
  イベントは 0 ルール・no-op（allow, stdout なし）になる——`PostCompact` は
  2026-09-05 に**削除済み**（PE-20）: ハーネスは `PostCompact` のフック出力を
  モデルへ一切見せない（デバッグログのみ）うえ判断フィールドも持たず、出力
  チャネル自体が存在しない。アンカーの再提示はコンパクション後の
  `SessionStart`（`source=compact`）で既に行われており冗長だった。
- 各イベントは `{tools, guards[]}` のルール配列で、`tools`（`tool_name` への正規表現、
  `tools_named` で `regex.named_alternations` の名前を参照することも可）がマッチした
  ルールの `guards[]` を **順に in-process 呼び出し**（ガードごとに子プロセスを立てない
  ——旧 PowerShell 版は 1 回の Bash 呼び出しで最大 5 プロセスを spawn していた）。
  最初に `exit_code == 2` を返したガードでブロックし、その `stderr` を表示する。
- 全ガードは `hooks/guards/<id>.py` の `run(payload, cfg, ctx) -> Result` という
  同一シグネチャに従う純粋関数。ポリシー（パス・閾値・メッセージ・正規表現）は
  一切ハードコードせず、すべて `config.json` から読む。

これらの詳細（ガード interface・`lib/` API・`config.json` スキーマ・不変条件・
実装時に誤りやすい箇所）は本 README の対象外——**`docs/DESIGN.md`**
（実装者向け設計ノート。これ 1 本 + `config.json` を読めばガードを書ける／変更できる）
を見ること。本 README は「何が存在し、何がどこに強制されているか」の見取り図だけを持つ。

> なお、docstring や `config.json` の `_note` に残る `CONTRACT.md §n` という参照は、
> `docs/DESIGN.md` の元になった内部契約書（本配布物には含まれない）の節番号である。
> §0–§4 は同番号で `docs/DESIGN.md` に引き継がれている。§5 以降（テスト規約・
> PE 表・レビュー履歴）は内部記録にのみ存在し、うち `PE-n` は
> `docs/PARITY-EXCEPTIONS.md` に公開されている。対応する節が無い §n、および
> `F-n` / `M-n` / `I-n` などのタグは、内部レビュー由来の不透明なラベルとして読む。

`~/.claude/skills/ulthar/` 配下がマスター（人間・エージェントが編集する原本）、
`~/.claude/hooks/` 配下が `install.py` によるデプロイ済み派生物であり、
**直接編集しない**（編集しても次の `install.py` 実行で上書きされ、
`install.py --verify` は master/deploy の drift を検出して exit 1 にする）。

| Guard (`hooks/guards/<id>.py`) | 発火点 (`config.json` の `dispatch.events`) | 強制内容 |
|---|---|---|
| `tool_discipline` | PreToolUse Bash/PowerShell | cd 複合禁止・シェルでのファイル作成禁止・削除は許可リスト（`pm-delete-allow`）経由のみ・オーナー権限ファイルへのシェル経路禁止 |
| `git_discipline` | 同上 | git 運用規律（保護ブランチ・destroyer 系コマンド・hook バイパス・force push 等） |
| `pr_state` | 同上 | PR 状態規律（`gh pr list` 照会） |
| `dev_server` | 同上 | 例: `npm run dev:heavy` はヒープ指定必須（OOM 再発防止、既定はルール空でオーバーレイで追加） |
| `grant_files` | PreToolUse Write/Edit/MultiEdit/NotebookEdit | オーナー権限ファイル（`grants.dir` 配下・`pm-delete-allow`・user/project オーバーレイ）への全エージェント書き込み禁止——自己付与の遮断 |
| `deploy` | PreToolUse Write/Edit/MultiEdit/NotebookEdit | `~/.claude/hooks/` 配下（`.ps1`/`.py`/`config.json`）への直接書き込み禁止——master/deploy drift の再発防止 |
| `commander` | 同上 | PM の直接実装禁止領域 |
| `skill_anchor` | 同上 / SessionStart | スキル要点の注入（`SessionStart` は生テキスト、他イベントは JSON envelope）。`PostCompact` への登録は 2026-09-05 に削除（PE-20、出力チャネルが存在しないため） |
| `brief_gate` | PreToolUse Agent/SendMessage | ブリーフ 6 ゲート: STATE 参照 / UI デザインスキル義務（`design_cases`、config 1 行で増減可） / OWNER-CONFIRMED リスト / 転送役の .out 出力先 / コミッタの Files: リスト / APPROVAL ハンドシェイク（提示記録 `~/.claude/state/brief-approvals/` ＋ 180 秒待機 or OWNER-OK） |
| `artifact_hygiene` | Stop | 成果物ディレクトリの掃除確認 |
| `dev_process_hygiene` | Stop | 開発プロセス残骸の確認（psutil 経由） |
| `stop_dev_orphans.py` | 手動 CLI（ガードではない、`dispatch.events` 未登録） | dev チェーン停止（`--all` は他サーバ生存時に使用禁止） |

## オーナー付与（`grants`）

ルールを 1 本だけ、1 回だけ免除する仕組み。識別子はオーバーレイの `disable` と同一構文
（`<dotted config path>/<rule id>`）で、寿命だけが違う（`disable` = 恒常／`grant` = 使用時消費）。
実体は**空ファイル 1 個**で、そのパスがそのまま識別子になる：

```
~/.claude/state/ulthar-grants/git_policy.force_push/push-force
```

- ブロックを決めたガードが `lib/grant.py:check()` を呼ぶ。付与があればそのルールを
  `continue`（**ALLOW ではない**——後続ルールを飛ばすと 1 本の付与が全部を通してしまう）。
- 常に main chain 限定。消費は 1 回。消費・subagent 無視・レース敗北はすべて
  `grants.log` に記録。存在しないルール名・`never_exempt` のルール名は次のツール呼び出しを
  BLOCK（`disable` の空振りと同じ扱い）。`never_exempt` のルールはオーバーレイの
  `disable` でも切れない（user / project どちらの層からも拒否。`lib/ctx.py:_refuse_never_exempt`）。
  付与より寿命の長い `disable` だけ素通りしていたため 2026-09-06 に閉じた。
- **`overlays.pinned_paths`**：どちらの層のオーバーレイからも書き換え不可・`disable` 不可の
  **セクション**（`dispatch` / `grants` / `regex` / `overlays`）。いずれもルールではなく
  「ルールに何ができるかを決める」設定。葉パス 4 本を列挙した初版では届かない経路が実測で 5 件
  出たためセクション単位にした（子パス `disable`、`regex.named_alternations` の付け替え、
  `grants.enabled:false`、`grants.dir` 移動、`grants.protected_message` の型崩し＝ガード例外→
  fail-open）。`lib/ctx.py:_refuse_pinned_paths` がベース config から読み、書き換えと
  `<prefix>.` 前方一致の `disable` の両方を拒否する。変更は `config.json` 本体で行う。
  代償：`regex.named_alternations` の拡張（編集系ツール名の追加）もオーバーレイからは不可。
  セクション消去（`{"command_rules": 0}` 等）もコンテナ入れ替えの拒否
  （`_refuse_container_swap`）の対象に入った。空オブジェクトはこの拒否には当たらず、
  `_merge_walk` がそのまま再帰する no-op として扱われる。さらに `_apply_disables`
  の後に `_refuse_authority_erasure` が走り（disable 適用後の config に対して判定）、
  `grants.never_exempt` の全識別子と
  `grants.protected` の全参照をマージ後の config に対して解決し直す。1 件でも解決できなければ
  該当オーバーレイファイル名付きで `ConfigError`（マージ結果そのものに対する不変条件）。
  これらはいずれも「設定ファイルによる事故的な弱体化」に対する境界であり、シェルを持つ
  エージェントに対する境界ではない——同じ `cp`/`tar -C`/`git clone`/`python -c open()` の経路は
  配備済みランタイムと `config.json` 本体そのものにも届く。`deploy_guard` は Write/Edit
  ツールのみを見ており、シェル経路はそもそも対象外。
  (a) オーバーレイがマージされたディスパッチでガードが例外を投げた場合、それはもう fail-open
  ではなく **BLOCK**（オーバーレイのファイル名を出す）。復旧はオーバーレイの JSON が壊れて
  いる場合と同じ——Claude Code の外（エディタや別ターミナル）から該当ファイルを削除する。
  (b) 未宣言（`overlays.list_merge` に載らない）リストの置換も、ベース要素のシェイプ
  （型・リストのアリティ・オブジェクトのキー）に合わなければマージ時に拒否される
  （`_check_replacement_conforms`）。
  (c) `lib/paths.py:is_under` は字句比較が偽のとき realpath で再判定して True に覆すことが
  あるが（8.3 短縮名対策）、どちらかの側が UNC パスか存在しないドライブを指す場合はこの
  再判定を行わない——遅いネットワーク往復を避けるため。
- **コンテナ入れ替えの拒否**（`lib/ctx.py:_refuse_container_swap`）：ルールリストの位置に
  dict/スカラーを置くと `_set_dotted` がリストごと差し替え、`tool_discipline` の全ルール
  （`authority-file-in-shell` を含む）が消えていた（実測 rc=0）。**リストはリストとしてのみ、
  セクションはセクションとしてのみ、スカラーはスカラーとしてのみ**マージ可。3 つ目
  （スカラーの位置にコンテナ）だけは結果が fail-open ではなく**ディスパッチャの異常終了**：
  `skill_anchor.envelope_key` は `dispatch.py:_notice_stdout` が `_run_guard` の try/except の
  **外**で読むため、オーバーレイ在席時のブロック規則では捕まらず、実測 rc=1・traceback、
  しかも project overlay の disable 通知マーカーは消費済みで通知も失われていた。
  通常の append とスカラーの上書きは従来どおり。
- **未宣言リストの置換もシェイプ検査**（`_check_replacement_conforms`）：`list_merge` に
  戦略が無いリスト（`redirect_noise_strips` / `quote_strip_patterns`）は user 層が丸ごと
  置換できるが、ベース要素の型・アリティ・キーに従う必要がある。これらは probe の生成に
  使われるので、置換は**通常ルール**が何を見るかを変える（オーナーが許可した `disable` と
  同等）。`target: raw` の権限 2 ルールには届かない。
- **オーバーレイ在席時のガードクラッシュはブロック**（`dispatch.py:_attribute_to_project_overlay`）：
  マージ済みオーバーレイが 1 つでもあれば、ガードの実行時例外は fail-open ではなく exit 2
  （オーバーレイのファイル名・ガード名・例外型を表示、復旧文は `dispatch.infra_recovery.overlay`）。
  オーバーレイが無ければ従来どおり fail-open（ULTHAR 自身のバグでツール呼び出し全体を
  止めない方が悪くないため）。マージ時のシェイプ検査は「導出できる範囲」を早期に拒否する
  最適化で、保証を担うのはこちら。
- **Win32 の末尾ドット/空白**：`lib/paths.py:norm_for_compare` が各コンポーネントの末尾
  `.`/空白を除去する。`normpath` はこれを残すが Win32 は捨てるため、`.claude./state/...` が
  別パス扱いになり、オーバーレイ無しの Write 1 回で付与ファイルを作れていた（実測 rc=0）。
  シェル側パターンにも同じ許容を入れた。POSIX は対象外（末尾ドットは通常の文字）。
- **自己付与の遮断**：`grants.protected` が指すファイル（付与ディレクトリ・`pm-delete-allow`・
  user/project オーバーレイ）は `grant_files`（Write/Edit）と
  `command_rules.tool_discipline/authority-file-in-shell`（シェル）で全エージェントに書き込み禁止。
  保証できない範囲は `docs/DESIGN.md`「What owner grants do not guarantee」に明記。
- 消費の勝者を決めるのは `os.remove` では**ない**（Windows では 2 スレッドの競合で両者成功が
  300 試行中 144 回＝実測）。`os.open(<grant>.claim, O_CREAT|O_EXCL)`（300/300 で単独勝者）で決める。

## ガードを 1 本追加する手順

1. `hooks/guards/<new_id>.py` に `run(payload, cfg, ctx) -> Result` を実装する
   （`docs/DESIGN.md` §1 の guard interface / hard rules に従う）。
2. そのガードが使う設定を `config.json` に追加する（`docs/DESIGN.md` §3 のスキーマ）。
3. `config.json` の `dispatch.events.<Event>[]` に `guards` として id を追加する
   ——**`settings.json` は一切変更しない**。
4. `hooks/tests/test_<new_id>.py` とフィクスチャを追加し、
   `python hooks/tests/run_all.py` を通す。
5. `python hooks/install.py` → `python hooks/install.py --verify` でデプロイと
   drift 検出を確認する。

## 運用
- 配布: `python install.py`（`config.json` + `lib/` + `guards/` ツリーをデプロイ。
  `hooks/tests/` はデプロイ対象外——スキル内に永続し、`--verify` はスキル側で実行する）。
  `lib/` / `guards/` は**ディレクトリ単位の置き換えではなくファイル単位の上書き**であり、
  配布先の同ディレクトリ内に元からあったファイル（他ツール・ユーザ自身のもの）は
  そのまま残り、`PRESERVED:` 行として stdout に列挙される
- 照合: `python install.py --verify`（master/deploy の hash 比較 + psutil 依存チェック
  + `cd-compound` フィクスチャによるスモークテストで exit 1/0 を判定）。
  master に対応の無い配布先ファイルは `EXTRA:` として**報告のみ**（FAIL にはしない）
  ——本スキルが配置していないファイルと、旧コピーセットの残骸とを
  ディスク上で区別する手段が無いため。欠落・不一致は従来どおり FAIL
- 撤去: `python install.py --uninstall`（`settings.json` の登録解除は手動。
  削除対象は COPY_TABLE が配置したファイルちょうどで、配布先に元からあるファイル
  ——サードパーティのフックやロールバック用 `*.ps1` 一式、`lib/` / `guards/` 内の
  第三者ファイルを含む——には触れず `KEPT:` として列挙する。例外は
  `__pycache__` / `*.pyc`（実行中の dispatcher が生成する再生成可能キャッシュ）で、
  これは削除する。空になった `lib/` / `guards/` は削除される）
- 文字コード: 全ファイル UTF-8。stdin/stdout/stderr は起動時に UTF-8 へ明示的に
  reconfigure 済み（旧 PowerShell 版の cp932 誤解釈・BOM 問題はこの層で解消済み——
  詳細は `docs/DESIGN.md` の `lib/io.py` / Encoding の項）。
