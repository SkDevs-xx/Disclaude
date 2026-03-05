# Heartbeat Checklist

このファイルに厳密に従うこと。推測や過去の会話からタスクを作り出さないこと。
報告事項がなければ `HEARTBEAT_OK` だけを返すこと。

## State
last_updated: 2026-03-05
wrapup_done: true
wrapup_time: "05:00"
last_wrapup_compressed: 2026-03-01
last_weekly_compressed: 2026-03-01

## 毎回チェック
- [ ] CURIOSITY.md の「未調査」に項目があれば 1件調べて memory/curiosity/ に記録する

## Wrap-up 確認
- [ ] wrapup_done が false かつ現在時刻 >= wrapup_time → `WRAPUP_NEEDED` を返す
- [ ] wrapup_done が true → 報告不要（HEARTBEAT_OK に含める）

## 応答ルール
- すべて問題なし、報告事項なし → `HEARTBEAT_OK` のみ
- 報告事項あり → 内容を返す（HEARTBEAT_OK は含めない）
- REVIEW.md に未レビュー項目があれば件数を報告し、`/review` を案内する
- `WRAPUP_NEEDED` は他の報告と併用可
