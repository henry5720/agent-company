# Deploy directly on the Fedora host

**Amended by [0014](0014-run-repo-code-in-the-prototype-sandbox.md)：** host 已換成有 sudo 的 Ubuntu，防火牆規則與 host proxy 要 root 管理。uid、`:z` 與 snapshot root 的做法不變。

Deployment改成直接在實體host上、以維護者自己的帳號執行Compose，不再包一層Incus instance。ADR-0002的前提是「不必給deployment identity實體host的sudo或Docker權限」，但該帳號本來就在`docker` group裡，那等同root，Incus那層擋不住一個已經存在的權限。維持它的代價是每次維護都要經過`incus exec`，而且instance內的docker、git、python與systemd要各自維護。改成直接部署換到單層runtime，代價是要處理宿主環境的三件事：container使用者的uid、SELinux label與排程機制。

## Consequences

- Container使用者的uid/gid必須等於執行docker的host使用者，由`Dockerfile`的`HOST_UID`／`HOST_GID` build arg設定，`.env`提供值。換機器或換帳號時要一起改並重新build。
- 所有bind mount帶`:z`，SELinux enforcing的host才讀得到；非SELinux的host上是no-op。`z`會relabel來源目錄，所以snapshot root必須是專用目錄，不能指向開發用的checkout。
- Snapshot root從`/srv`移到維護者的home，因為沒有sudo就不能寫`/srv`。
- 排程機制改變，見[0004](0004-schedule-snapshots-with-cron.md)。
- Docker逃逸後直接落在實體host上的維護者帳號，不再停在Incus邊界內。這是接受的取捨，理由是該帳號的`docker` group成員資格本身已經等同root。
