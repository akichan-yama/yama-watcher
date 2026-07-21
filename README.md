# 山小屋 空室監視ボット（GitHub Actions版）

雲ノ平山荘・双六小屋・三俣山荘の3つの空室状況を1時間おきに自動チェックし、
空きが出たらメール（＋任意でntfy.shのプッシュ通知）で知らせます。
GitHub Actionsのクラウド上で動くため、**PCの電源を入れておく必要はありません**。

```
yama-watcher/
  tenawan/          雲ノ平山荘
    watch_vacancy.py
    config.yaml      ← 監視したい日付をここで指定（最大3件）
  sugorokugoya/      双六小屋
    watch_futamata.py
    config.yaml      ← 小屋名・部屋タイプ・日付をここで指定
  mitsumata/         三俣山荘
    watch_mitsumata.py
    config.yaml      ← 監視したい日付をここで指定（最大3件）
  .github/workflows/ ← 1時間ごとの自動実行設定（触らなくてOK）
  requirements.txt
  .env.example       ← ローカルテスト用のひな形
```

---

## 1. GitHubアカウントを作る（まだなければ）
https://github.com/ で無料登録します。

## 2. リポジトリを作る
1. GitHubにログイン → 右上「+」→「New repository」
2. Repository name: `yama-watcher`（何でもOK）
3. **Public**（公開）のままでOK（無料枠が無制限になります。秘密情報はSecretsで守られるので安全です）
4. 「Create repository」

## 3. このフォルダの中身をアップロード
- 一番簡単な方法：GitHubのリポジトリ画面で「Add file」→「Upload files」から、
  このフォルダの中身（`.github`フォルダも含めて）をまとめてドラッグ＆ドロップ
  - ※`.env`ファイルはアップロードしないでください（これはローカル専用です。そもそも
    `.gitignore`に登録済みなので、Gitを使った方法ならアップロードされません）
- Gitに慣れている場合は通常の `git init` → `git add .` → `git commit` → `git push` でもOKです

## 4. Secrets（秘密情報）を登録する
1. リポジトリ画面 →「Settings」タブ
2. 左メニュー「Secrets and variables」→「Actions」
3. 「New repository secret」で、以下を1つずつ登録
   | Name | Value |
   |---|---|
   | `GMAIL_ADDRESS` | `sahashi2@gmail.com` |
   | `GMAIL_APP_PASSWORD` | Gmailのアプリパスワード（16桁） |
   | `TO_ADDRESS` | `sahashi2@gmail.com` |
   | `NTFY_ENABLED` | `true`（使わない場合は `false`） |
   | `NTFY_TOPIC` | 自分だけのntfyトピック名 |

## 5. ワークフローに書き込み権限を与える
1. 「Settings」→左メニュー「Actions」→「General」
2. 一番下「Workflow permissions」で
   **「Read and write permissions」を選択**して保存
   （状態ファイルをリポジトリに書き戻すために必要です）

## 6. 動作確認（手動実行）
1. リポジトリ画面の「Actions」タブを開く
2. 左側に3つのワークフロー（雲ノ平山荘 空室監視／双六小屋 空室監視／三俣山荘 空室監視）が表示されます
3. 1つ選んで「Run workflow」ボタンから手動実行
4. 実行結果（ログ）が正常終了（緑のチェック）になるか確認
   - 失敗する場合は、ログを開いてエラー内容を確認してください（Secretsの入力ミス等が多い原因です）

これ以降は、それぞれ1時間おきに自動実行されます（雲ノ平: 毎時0分、双六小屋: 毎時5分、
三俣山荘: 毎時10分。GitHub Actionsの仕様上、時刻ちょうどには実行されず数分ずれることが
あります）。

---

## 監視対象日の変更方法（設定ファイル）

各フォルダの `config.yaml` を編集するだけです（最大3件まで指定できます）。

**雲ノ平山荘** (`tenawan/config.yaml`):
```yaml
target_days:
  - month: 8
    day: 29
  - month: 8
    day: 30
  - month: 8
    day: 31
```

**双六小屋** (`sugorokugoya/config.yaml`):
```yaml
target_hut: "双六小屋"
target_days:
  - 26
  - 27
  - 28
target_rooms:
  - "一般室"
  - "個室(2名)"
  - "個室(2～3名)"
```

**三俣山荘** (`mitsumata/config.yaml`):
```yaml
target_days:
  - 28
  - 29
  - 30
```

編集してGitHubにアップロード（コミット）すれば、次回の実行から新しい設定が反映されます。
GitHubの画面上で直接ファイルを開いて鉛筆マークで編集→「Commit changes」でもOKです。

---

## ローカルPCでのテスト方法（任意）

クラウドに上げる前に手元で試したい場合:

1. ルートフォルダの `.env.example` を `.env` にコピーして値を入力
2. ```
   python -m pip install -r requirements.txt
   ```
3. 各フォルダで
   ```
   python watch_vacancy.py --debug
   python watch_futamata.py --debug
   python watch_mitsumata.py --debug
   ```

## 通知の仕組み
- 空きを検知すると、その回のチェックで新たに空いた日付・部屋をまとめて1通のメール／ntfy通知にします
- 一度通知した状態のままなら再通知しません。満室に戻ってから再度空いた場合は、また通知します
- 状態は各フォルダの `state.json` に保存され、GitHub Actionsの実行のたびにリポジトリへ自動コミットされます（コミット履歴に残りますが、動作には支障ありません）

## トラブルシューティング
- **Actionsが失敗する**: 「Actions」タブ→失敗した実行→ログを開いてエラー内容を確認。多くはSecretsの入力ミスです。
- **通知が来ない**: 対象サイトのHTML構造が変わり抽出に失敗している可能性があります。ログの「状態を特定できませんでした」を確認し、`debug_page.html`相当の内容を教えてください（ローカルの`--debug`実行で確認できます）。
- **三俣山荘サイトへのアクセスがブロックされる**: ボット対策が強いサイトのため、GitHub Actionsのサーバーからのアクセスが弾かれる可能性があります。その場合はログのエラー内容を教えてください。
