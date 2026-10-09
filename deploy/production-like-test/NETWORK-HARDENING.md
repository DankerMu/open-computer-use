# 网络与隔离加固说明

## 部署入口与端口边界

先构建并导入一份内容校验过的离线发布，再 bootstrap 和启动。权威清单是 `release.json`，不是 `DEPLOYED_VERSION.md`。

```text
python3 deploy/release.py build --ocu-source <committed-ocu> --webui-source <committed-webui-fork> --destination <new-release-dir>
python3 deploy/release.py import --delivery <release-dir> --install-root <DEPLOY_ROOT>
OCU_RELEASE_MANIFEST=<DEPLOY_ROOT>/release.json SOURCE_SHA=<ocu-full-sha> ... deploy/production-like-test/scripts/bootstrap-test.sh
OCU_RELEASE_MANIFEST=<DEPLOY_ROOT>/release.json deploy/up.sh
OCU_RELEASE_MANIFEST=<DEPLOY_ROOT>/release.json deploy/production-like-test/scripts/write-deployed-version.sh
```

`build` 从干净的已提交 OCU/WebUI 快照构建五张源镜像并拉取选定的 PostgreSQL 和 DocumentServer（已缓存且 identity 匹配的上游引用可复用，不覆盖不同内容；更新缓存上游标签是显式操作），导出 linux/amd64 命名归档和 OCU Git bundle。内容派生的 workspace 名称必须保留 `open-computer-use`，并使用完整 configuration digest 作为标签。清单记录两个完整源 SHA、所选源的 `source_consumer_contract`、七张镜像的命名引用与 configuration digest（Docker image `.Id`，不是 registry manifest digest）、归档 SHA-256、非密钥 build-arg 以及材料输入版本/哈希。输入哈希不是已安装软件包版本。缺少该契约常量的历史 checkout（例如 `f851621`）不是合格离线运行时。

`import` 在加载任何镜像前校验 schema、七角色集合、受约束的普通文件路径、全部校验和、归档内部 Docker/OCI 引用与配置字节、所选源消费者契约以及已有本地标签冲突。检查和 `docker load` 使用同一份私有暂存字节。合作构建/导入对同一本机 Docker 守护进程的镜像库写入通过 `/run/ocu-image-store/<daemon-id>.lock` 串行化；该 flock 文件在进程生命周期内持有、不在等待者仍引用其 inode 时 unlink。远程 Docker 守护进程协调不受支持。失败不发布安装根或成功记录；一旦尝试过 load，会披露可能残留的镜像缓存，但不会删除无关镜像。已有目标目录和竞争发布锁都会失败。任意外部 Docker 写者与宿主机管理员不在该保证内。

`deploy/up.sh` 在任何网络/防火墙/容器变更前要求 `OCU_RELEASE_MANIFEST`，并核对本仓库 HEAD、被消费的 tracked 部署/initializer 字节、七张本地镜像以及解析后的服务镜像（含 `open-webui-init` 复用 `open-webui`）。无关 untracked 文件单独存在不是拒绝原因。冻结快照以 `up -d --no-build --pull never` 启动；缺少或被替换的本地镜像不会触发 build 或 pull。`bootstrap-test.sh` 仍保持 root-only、0600、不覆盖既有 runtime/凭证；镜像值可从清单安全派生，但不会执行生成的 shell 片段。

Only release inventory `format_version` 2 is accepted; version 1 is refused by
import, verify, startup and recovery. DocumentServer is pulled from the immutable
upstream reference declared in the selected source's `deploy/release.py`, not built
from a derived Dockerfile. Its existing provenance `build.dockerfile` field names
that declaration file. The upstream index digest is distinct from the inspected
linux/amd64 configuration digest and the exported archive checksum.

Format 2 requires `font_bundle: {path, sha256}`. Build reads the selected committed
source's `deploy/fonts/fonts.json`, verifies upstream ZIP sizes and hashes, and
packages only its pinned fonts and licenses into `fonts.tar`. Import and delivery
verification reject missing, extra, duplicate, linked, or changed members. Import
publishes `fonts/` beside `release.json` without retaining the transport archive.
Fontless inventories are refused, including format-2 inventories.

Before any mutation, `up.sh` verifies the installed font directory against that
selected source and exports its inventory-relative absolute path as
`OCU_RELEASE_FONTS_DIR`, overriding inherited values. Do not persist this derived
path in runtime configuration. Font binaries are release material, not tracked
source files; unrelated existing application fonts remain unchanged. This
preflight does not establish renderer consumption or DocumentServer readiness.

Bootstrap requires `ENABLE_OCU_OFFICE_EDIT=true|false` and
`OCU_OFFICE_DOCSERVER_ORIGIN` explicitly, even with editing disabled. The latter
must be an absolute HTTP(S) origin distinct from WebUI, including its effective
port. `OCU_OFFICE_PROXY_PORT` defaults to `8083`; `OCU_OFFICE_FONTS_DIR` defaults
to `<DEPLOY_ROOT>/data/office-fonts`. Explicit empty values are errors. Bootstrap
creates an absent operator-font directory empty and leaves existing contents and
metadata unchanged.

Both flag values provision the same settings: DocumentServer at
`http://documentserver`, OCU callbacks at `http://computer-use-server:8081`, and
one fresh JWT secret in the mode0600 runtime file only. Existing runtime/admin
outputs are never overwritten and no replacement secret is generated for them.
These settings do not certify compose wiring or a running DocumentServer.

WebUI overlay 打开既有 `OFFLINE_MODE` / `ENABLE_VERSION_UPDATE_CHECK=false` / 模型自动更新关闭开关，并保留已配置的 LAN OpenAI/RAG 端点与本地 Draw.io/Pyodide 材料。实际 Docker 引擎导入、平台/entrypoint 兼容和断网重启验收属于 #36；本源码阶段的 fake CLI 证据不能关闭 #33。

失败边界：损坏或越界归档、符号链接、未声明或冲突的归档内部标签、隐藏 OCI 别名、配置 digest 冲突、脏的 tracked 源、不兼容的源消费者契约、缺失/替换的本地镜像、错误平台、密钥 build-arg、已有安装根、不受支持的远程 Docker 守护进程。

支持的归档形式限于经典 Docker `manifest.json`/`repositories` 以及 Moby/containerd 导出的单目标平台 hybrid（`oci-layout` + `index.json` + `manifest.json` + content-addressed blobs）。嵌套 index 与 exporter 添加的 attestation/referrer 必须被显式核算；不支持的 metadata 在 load 前拒绝。

从源码 checkout 根目录配置运行环境后运行 `deploy/up.sh`。入口依次解析 core、WebUI、proxy 三套 Compose 配置为私有临时 JSON，运行 `deploy/check-ports.sh` 与 `deploy/provision-networks.sh`，再通过 `run_owned` 运行 `deploy/check-sandbox-dns.sh`、`deploy/firewall/docker-user-rules.sh` 与 `deploy/firewall/check.sh`，然后用已检查的快照启动 core 和 WebUI，最后启动 proxy。core/WebUI 的项目目录是源码根；proxy 的项目目录是 overlay，以保持 `../proxy` 构建上下文。启动时覆盖 `COMPOSE_REMOVE_ORPHANS=false` 和 `COMPOSE_PROFILES=`，避免拆除共享项目中的兄弟栈或激活 cleanup。nginx 在配置校验时解析 `open-webui:8080` 和 `computer-use-server:8081`，因此两个应用必须先存在。入口监督配置解析、快照冻结、检查、建网、DNS 预检、防火墙安装和启动子进程；TERM/INT/HUP 会结束所属进程组并删除私有临时文件，但不会 `down`、删除卷、迁移网络、刷新共享防火墙链或修改现存 sandbox。

- 只有 proxy 将 `${OCU_PROXY_PORT}` 映射到容器的 TCP 8082。WebUI、Computer Use、PostgreSQL、initializer 和维护服务不得发布宿主机端口，即使只绑定 loopback 也不允许。
- 应用服务仅连接由 `${OCU_PRIVATE_NETWORK}` 命名、`${OCU_PRIVATE_SUBNET}` 与 `${OCU_PRIVATE_GATEWAY}` 定址的 control-plane bridge；proxy 通过同一 bridge 的 Docker DNS 找到应用。显式 `network_mode`（包括 `bridge`）不得代替该命名网。
- `${OCU_SANDBOX_NETWORK}` 是部署入口单独创建或校验的非 internal bridge，具有 `${OCU_SANDBOX_SUBNET}` 和 `${OCU_SANDBOX_GATEWAY}`。Compose 服务不加入此网络。OCU 将 CDP/ttyd 动态端口只绑定到该 gateway，且原生网络策略只允许 sandbox 连接这张 bridge。
- Open WebUI 必须设置 `ENABLE_OCU_WORKSPACE=true` 和 `OCU_INTERNAL_URL=http://computer-use-server:8081`；`ORCHESTRATOR_URL` 不是客户端别名。
- 运行环境必须显式提供 `OCU_RELEASE_MANIFEST`、`OCU_PRIVATE_NETWORK`、`OCU_PRIVATE_SUBNET`、`OCU_PRIVATE_GATEWAY`、`OCU_SANDBOX_NETWORK`、`OCU_SANDBOX_SUBNET`、`OCU_SANDBOX_GATEWAY`、`OCU_SANDBOX_EGRESS_ALLOW`、`OCU_SANDBOX_DNS`、`OCU_PROXY_PORT`、`OCU_PROXY_IMAGE`、`OCU_INTERNAL_TOKEN`、`OCU_WEBUI_ORIGIN`、`OCU_WEBUI_AUTH_URL` 和 `PUBLIC_BASE_URL`，以及既有应用和 provider 的必要变量。`OCU_SANDBOX_EGRESS_ALLOW` 未设置是配置错误；显式空值表示拒绝全部新的 sandbox 出站。`OCU_SANDBOX_DNS` 未设置是配置错误；显式空值通过容器本地 `127.0.0.11` 覆盖关闭外部 DNS 转发，而不是继承宿主机 nameserver。不要将 token 写在命令行、日志或仓库文件中。`scripts/bootstrap-test.sh` 要求 `OCU_RELEASE_MANIFEST`、匹配 checkout 的完整 `SOURCE_SHA` 和 `OCU_WEBUI_ORIGIN`；六张镜像引用必须与清单一致，或从清单安全派生。它写入上述拓扑/鉴权变量、`PUBLIC_BASE_URL=${OCU_WEBUI_ORIGIN}/ocu`、内部 `OCU_WEBUI_AUTH_URL=http://open-webui:8080/api/v1/ocu/auth`、共享内部 token、`OCU_PUBLIC_PREFIX=/ocu` 与 `OCU_SANDBOX_NO_AUTOSTART=1`。


`deploy/check-ports.sh` 的输入是 **完整的** `docker compose config --format json` 输出集合，不能用原始 YAML 代替；`expose` 不发布端口。bridge 已存在但 driver、internal 模式、subnet 或 gateway 不匹配时入口拒绝启动，不删除、替换、断开网络或现存 sandbox。防火墙安装绑定权威 sandbox 网桥入口接口，而不是源地址；同一宿主机只维护一份 owned 策略。安装器与检查器通过 `OCU_SANDBOX_EGRESS_LOCK`（默认 `/run/ocu-sandbox-egress/ocu-sandbox-egress.lock`）串行化合作进程；该路径必须位于当前 euid 拥有、非符号链接、非 group/world-writable 的目录中。DNS 解析、真实镜像构建、Compose 合并、内核数据包路径和引擎端口矩阵的实际验收留给 #36。

部署完成后不要把 overlay smoke 接到 `deploy/up.sh`。在同一已配置的部署 shell 中运行 `deploy/smoke.sh`：Compose 插值变量与 `up.sh` 相同，另需显式 `OCU_SMOKE_CHAT_ID`、`OCU_SMOKE_SANDBOX_ID`、`OCU_SMOKE_EXCLUSIVE=1`、`OCU_SMOKE_OWNER_TOKEN`（只进进程环境，不得出现在 argv/日志）、IPv4 字面量 `OCU_SMOKE_FORMER_URL`、`OCU_SMOKE_EGRESS_URL` 与 `OCU_SMOKE_HOST_LAN_IPV4`。命令核验三套 Compose `ps --all` 清单（仅 proxy 发布 TCP `${OCU_PROXY_PORT}:8082`；`computer-use-server`/`open-webui`/`proxy`/`postgres`/`retention-guard` 必须在跑；oneshot 可缺席但其 publication 仍检查；运行中的 `cleanup` 失败）、前 OCU 入口 `ECONNREFUSED`、宿主机对 control 字面量的 HTTP 存活、sandbox 内允许列表 HTTP 2xx/3xx，然后才接受 sandbox 对 OCU:8081/WebUI:8080/proxy:8082 与宿主机 LAN proxy 的 curl 连接阶段超时（exit 28 且无 TCP 连接）。空允许列表是合法 deny-all，但不能完成本 smoke，以非零前提失败。终端只在独占、无既有 ttyd/tmux 的 smoke sandbox 上经 `dangerous_mode=false` 的 start-ttyd 与 `tty` WebSocket 观察产品 pane/前台 Bash；清理失败即失败。退出码：0 全部断言与 owned cleanup 完成；1 断言失败；2 前提/用法；129/130/143 为 HUP/INT/TERM。本地 fake/native 证据不是真实引擎验收；数据包、ttyd 镜像与宿主防火墙证明留给 #36。

## 已知边界

部署入口在建网之后、启动应用之前核对 sandbox DNS 并安装出站策略：IPv4 经 `DOCKER-USER` 与 `INPUT` 的接口挂钩，且 `FORWARD` 的第一条规则必须是无条件 `-j DOCKER-USER`；IPv6 经 `INPUT` 与 `FORWARD` 默认 DROP。允许列表只豁免本守卫，不绕过后续宿主机策略；control-plane 子网与 metadata `169.254.169.254/32` 即使被宽网段覆盖也仍 DROP。控制面发起的 CDP/ttyd 应答走 conntrack `REPLY`，sandbox 发起的 ORIGINAL 已建立流仍受当前允许列表约束。control-plane 网桥在首次 Compose 启动前可以不存在，安装器此时使用已校验的配置 CIDR；已存在的 control-plane 网仍按 #24 校验。DNS 预检拒绝继承宿主机 nameserver 或不兼容的受保护网桥容器 DNS，但不声称真实数据包隔离；该证据属于 #36。`computer-use-server` 仍挂载 Docker socket，是高权限可信组件。未经授权的直接 OCU 入口不得用作回滚方案。

## 正式离线生产建议

1. 将 proxy 置于内网 TLS、公司 SSO/身份源和 IP allowlist 后面，不公开 MCP 或动态 sandbox 端口。
2. 高风险环境采用独立 Docker host、VM 或 Kubernetes node；按风险模型评估 rootless Podman、gVisor 或 Kata Containers。
3. 对 sandbox 出站网络按业务需要做 allowlist；禁止访问 Docker API、metadata 服务、公司管理网及凭据系统。
4. 导入已校验的 `release.json` 与镜像归档；启动禁止 build/pull。断网后验证 WebUI、RAG 和 sandbox 工具，并同时检查全新与既有 WebUI 配置。

5. 对话、embedding、rerank 连接内网 endpoint；模型 Key 只注入 WebUI，绝不传入 OCU 或 sandbox。
6. 定期用 `deploy/recovery.py` 与 `deploy/BACKUP-RESTORE.md` 做冷备份和独立空目标恢复演练；监控 Docker data-root、PostgreSQL、WebUI volume 和 chat data 的磁盘用量。未跟踪的本地草稿不是该入口。
