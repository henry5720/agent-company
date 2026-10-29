# Schedule snapshots with cron

**Amended by [0014](0014-run-repo-code-in-the-prototype-sandbox.md)：** host 已換成有 sudo 的 Ubuntu，「沒有 sudo」的前提不再成立；cron 換回 systemd timer 不在那次範圍內，排程仍全部由 `scripts/lib.sh` 的 `render_crontab` 產生。

Snapshot同步改用維護者的crontab，不用systemd timer。裝system unit要sudo，而deployment帳號沒有；`systemctl --user`要開lingering，那同樣需要提權。cron是這台host上唯一不需要任何特權、又能在重開機後繼續運作的排程器。

一般而言systemd timer在systemd主機上是比較好的選擇——journald自動收log、`Persistent=true`能補跑錯過的排程、可以宣告service相依。這兩個好處在這個job上剛好都不重要：`scripts/update-snapshots.sh`用`flock`自己防重入，而且它是冪等的，錯過一次下一輪就補上。

## Consequences

- 沒有journald。`scripts/update-snapshots.sh`在非tty時自行寫入`$XDG_STATE_HOME/work-agent/snapshots.log`並保留一份`.1`輪替；手動執行時仍然印在terminal上。
- 沒有`Persistent=`補跑。開機後第一次同步要等到下一個整點。
- cron的環境極簡，所以`SNAPSHOT_ROOT`由`scripts/install-sync-cron.sh`寫死進crontab那一行，不依賴shell profile。
- 手動同步從`sudo systemctl start`變成直接執行`scripts/update-snapshots.sh`，不需要提權。
- **換回systemd timer的條件**：deployment帳號拿得到sudo，或拿得到`loginctl enable-linger`。滿足其一就值得換回去，主要是為了journald和`Persistent=`。舊的unit檔在git history裡（`git show 0251955:systemd/work-agent-snapshots@.service`）。
