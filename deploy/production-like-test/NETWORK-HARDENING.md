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

To upgrade a running deployment and keep its data, see [Upgrading an existing deployment](#upgrading-an-existing-deployment).

`build` uses clean committed OCU/WebUI snapshots to build six source images, including DocumentServer, and pull the selected PostgreSQL image. PostgreSQL is the only pulled role: the exact selected upstream reference is reused when cached, and a content-addressed output reference with a different image identity is rejected rather than overwritten. Changing the upstream reference is an explicit choice in the tracked compose declaration or via `--postgres-image`; the build does not force-refresh the same cached reference. It exports named linux/amd64 archives and an OCU Git bundle. The content-derived workspace name retains `open-computer-use` and uses the full configuration digest as its tag. The inventory binds both source commits, the selected `source_consumer_contract`, seven named image/configuration identities, archive SHA-256 values, non-secret build arguments and material input versions/hashes. Input hashes are not installed-package versions. Configuration identity comes from configuration bytes, not a registry manifest; engines may expose a manifest as image `.Id`. General containerd release-path compatibility remains tracked in [issue 337](https://github.com/DankerMu/open-webui/issues/337); local DocumentServer qualification does not certify the complete export/import path on that engine.

`import` 在加载任何镜像前校验 schema、七角色集合、受约束的普通文件路径、全部校验和、归档内部 Docker/OCI 引用与配置字节、所选源消费者契约以及已有本地标签冲突。检查和 `docker load` 使用同一份私有暂存字节。合作构建/导入对同一本机 Docker 守护进程的镜像库写入通过 `/run/ocu-image-store/<daemon-id>.lock` 串行化；该 flock 文件在进程生命周期内持有、不在等待者仍引用其 inode 时 unlink。远程 Docker 守护进程协调不受支持。失败不发布安装根或成功记录；一旦尝试过 load，会披露可能残留的镜像缓存，但不会删除无关镜像。已有目标目录和竞争发布锁都会失败。任意外部 Docker 写者与宿主机管理员不在该保证内。

`deploy/up.sh` 在任何网络/防火墙/容器变更前要求 `OCU_RELEASE_MANIFEST`，并核对本仓库 HEAD、被消费的 tracked 部署/initializer 字节、七张本地镜像以及解析后的服务镜像（含 `open-webui-init` 复用 `open-webui`）。无关 untracked 文件单独存在不是拒绝原因。冻结快照以 `up -d --no-build --pull never` 启动；缺少或被替换的本地镜像不会触发 build 或 pull。`bootstrap-test.sh` 仍保持 root-only、0600、不覆盖既有 runtime/凭证；镜像值可从清单安全派生，但不会执行生成的 shell 片段。

Only release inventory `format_version` 2 is accepted; version 1 is refused by
import, verify, startup and recovery. DocumentServer is built from the selected
source's `deploy/documentserver/Dockerfile`, immutable B1 base and exact patch
inventory. The recipe changes only the three native desktop bootstrap scripts
and adds a modification notice. It rejects source/hash drift and build overrides.
PostgreSQL remains pulled. The base index, derived platform manifest,
configuration digest and exported archive checksum are distinct identities.

`configuration_digest` hashes the saved configuration bytes, never an inspected
engine ID. Optional `descriptor_digests` contain only independently hash-verified
image manifests and ancestor indexes bound to that reference, configuration and
platform. Attestations, foreign siblings and other platforms cannot authorize an
engine identity. Registry observations and archive checksums have separate roles.
Attachment markers do not prove non-selectability. A marked manifest whose
verified configuration selects linux/amd64 refuses before load even if its
configuration equals the runtime image's and the inventory omits the index digest.
Verified unknown/unknown attachments remain excluded from runtime identities.
Named indexes containing foreign selectable configurations and attachment indexes
whose selection cannot be qualified are refused.
Build derives the content tag from the first export, retires that private archive,
saves the final reference and verifies the complete archive set before publication.
Startup, bootstrap and recovery accept an actual engine/container `Image` only
when it belongs to the selected configuration/descriptor set; they do not export
images to establish identity.

For containerd stores, observed daemon `DriverStatus` must identify the containerd
snapshotter and both client and server must expose API 1.49 or newer. Inspection
and export explicitly select `--platform linux/amd64`; an unfiltered multi-platform
index may lack platform fields or refer to unfetched content. Classic stores use
ordinary inspect/save and still require concrete linux/amd64 metadata plus archive
agreement. Unsupported containerd capability combinations refuse without retrying
an unfiltered export or assuming a platform. The selected engine ID in the version
record comes from the same verified inspection, not a raw root-index observation.

An optional Docker `repositories` entry associates its reference with the selected
manifest's final layer, not the configuration. The parser verifies that association
and the final layer's configuration diff ID using streamed hashing. Undeclared
references, inconsistent layer associations and altered layer bytes refuse before
load; shared layers are valid. Layer identities never authorize engine IDs.

New builds require selected committed source-consumer contract2 before Docker
mutation. Format2 readers accept contract1 only with config-only image records
and matching bundled source1, preserving valid classic-store rollback. Contract2
also accepts config-only records. Unknown contracts, contract1 descriptor fields
and mismatched bundled readers are refused. Historical artifacts that mislabeled
an engine ID as configuration must be rebuilt, not edited into apparent validity.
Direct `deploy/up.sh` invocation checks the inventory/source contract pairing
before deployment resource mutation, independently of prior bootstrap or verify.

### Qualified local image identity paths

| Local daemon/store | Build/export path | Observed engine identity | Evidence boundary |
| --- | --- | --- | --- |
| Docker Engine 29.1.3, classic `vfs` | `docker build --platform linux/amd64`, ordinary inspect/save | Configuration digest | Seven-role CLI protocol qualification and separate initially empty image/cache targets |
| Docker Engine 29.1.3, containerd snapshotter | linux/amd64 build; inspect/save explicitly select `--platform linux/amd64` | Selected runtime manifest IDs verified against exported descriptors | Seven-role CLI protocol qualification and separate initially empty image/cache targets |
| Docker Desktop Engine 29.1.3, containerd store | Build/save and load of an isolated image | Verified image-manifest digest | Producer/load/container identity observations; not full release CLI or product acceptance |

Both Engine profiles passed seven-role CLI build/export, delivery verification,
import, local/startup verification and identical re-import, including separate
cold targets with initially empty image stores and caches. The daemon binaries
were arm64 on LinuxKit; all release images were linux/amd64. Inputs used five
small protocol-image recipes plus the actual qualified DocumentServer and
PostgreSQL images. This qualifies the exercised release protocol, not production
application behavior or native-Linux portability.

Containerd's unqualified raw index ID can differ from the selected runtime
manifest ID. Platform-selected exports and verification bind the latter to the
actual configuration. A raw index ID is not accepted merely because the daemon
reports it; it must independently occur in the archive's verified identity set.

The release transport is limited to local Docker daemons and one linux/amd64
image per role. Pure classic Docker archives contribute no alternate descriptors;
canonical Moby/containerd hybrid archives can provide verified alternates on
either store. Qualification of one store/export combination does not qualify all
Docker versions, snapshotters or export tools. Remote daemons, Podman release
transport, Windows and multi-platform publication are not supported by this
change. The Kubernetes chart's inner Podman sandbox engine is a different path.
Production application-image behavior, WAN-free product restart and LAN
deployment require separate execution evidence. Neither the Engine protocol
qualification nor the standalone Desktop identity probes establish those results.


For local Office qualification, explicitly load the archive matching
`deploy/documentserver-qualified.json` and the selected recipe tree before running
the WebUI `make verify-office` target. Its content-derived reference and inspected
immutable identity must match the qualified platform entry. Missing material is
an error; the target does not build, pull or select a fallback image. Preserve the
original B1 image. This local campaign is not release-wide or LAN acceptance.

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

The core stack includes `documentserver` regardless of the WebUI editing flag.
It uses `DOCUMENTSERVER_IMAGE`, publishes no host port, and joins only the
control-plane network. The port guard requires it exactly once; its resolved
image must match the release inventory before provisioning or startup.

`up.sh` requires `DOCUMENTSERVER_IMAGE`, `OCU_OFFICE_JWT_SECRET`,
`OCU_OFFICE_DOCSERVER_URL`, `OCU_OFFICE_DOCSERVER_ORIGIN`, `OCU_OFFICE_SELF_URL`,
`OCU_OFFICE_PROXY_PORT`, `OCU_OFFICE_FONTS_DIR` and `ENABLE_OCU_OFFICE_EDIT`,
even when editing is off. It rejects an invalid flag, nondecimal or out-of-range
Office port, and an invalid browser origin using the same origin
and port validators as bootstrap. `OCU_OFFICE_FONTS_DIR` must name an existing
directory before any engine mutation; missing paths, dangling links and files fail.

DocumentServer receives `JWT_ENABLED=true` and `JWT_SECRET` from the bootstrap
secret. The selected upstream entrypoint applies these controls to browser,
inbox and outbox. Before provisioning or startup, admission checks the resolved
enablement and requires the resolved secret to match both the supplied
`OCU_OFFICE_JWT_SECRET` and the OCU service's resolved value. A failure names the
setting without printing a credential, preserves engine state and removes owned
temporary snapshots. Startup uses those checked snapshots, not a new resolution.

DocumentServer's own-data mounts are project-scoped named volumes:
`documentserver-data` at `/var/www/onlyoffice/Data`, `documentserver-cache` at
`/var/lib/onlyoffice`, and `documentserver-logs` at `/var/log/onlyoffice`.
Its only configured host binds are the two font directories below; no Docker
socket, chat, skills or workspace bind is attached.
The proxy is the sole publisher: `OCU_PROXY_PORT:8082` serves WebUI/OCU and
`OCU_OFFICE_PROXY_PORT:8083` serves DocumentServer, in both editing-flag states.
The second listener requires WebUI session authentication, strips cookies and
credential/identity headers before forwarding, and keeps the browser-facing
host/port and listener scheme. Its upstream comes from `OCU_OFFICE_DOCSERVER_URL`.

The release font directory mounts at `/usr/share/fonts/truetype/ocu-release`;
`OCU_OFFICE_FONTS_DIR` mounts at `/usr/share/fonts/truetype/ocu-operator`.
Both binds are read-only and refuse automatic host-path creation. Empty operator
directories and directory symlinks are accepted without modifying their contents
or metadata.

A relative operator path is relative to the `up.sh` invocation's working directory,
not Compose's project directory. The entry exports its absolute spelling for
checking and mounting; it does not rewrite stored configuration. Prefer an absolute
stored path for repeatable activation. On a restored host the operator must make
that configured directory available: it is not captured, remapped or created by
recovery. Recovery invokes the entry from the restored source root, which becomes
the base for any retained relative operator path.

The release-font bind follows the verified manifest-adjacent `fonts` entry,
including the selected-release link after activation. Do not store
`OCU_RELEASE_FONTS_DIR` or substitute the operator directory for that release material.

`release.release_fonts_directory` owns the lexical manifest-relative path rule.
Startup exports the path returned by its existing font verification; the smoke
uses the same pure rule for Compose inventory queries, without repeating content
verification. Both override an inherited release-font value. Ad-hoc Compose
commands that load the core model must also supply this derived input from the
selected manifest; do not persist or reuse it across release activation.

Overlay contract tests require a native Compose v2 resolver (`docker-compose` or
the system `docker compose` plugin). They resolve all three stacks using
synthetic inputs and isolated configuration; no daemon or image execution is
needed. Configuration checks do not certify runtime volume persistence.

WebUI overlay 打开既有 `OFFLINE_MODE` / `ENABLE_VERSION_UPDATE_CHECK=false` / 模型自动更新关闭开关，并保留已配置的 LAN OpenAI/RAG 端点与本地 Draw.io/Pyodide 材料。实际 Docker 引擎导入、平台/entrypoint 兼容和断网重启验收属于 #36；本源码阶段的 fake CLI 证据不能关闭 #33。

失败边界：损坏或越界归档、符号链接、未声明或冲突的归档内部标签、隐藏 OCI 别名、配置 digest 冲突、脏的 tracked 源、不兼容的源消费者契约、缺失/替换的本地镜像、错误平台、密钥 build-arg、已有安装根、不受支持的远程 Docker 守护进程。

支持的归档形式限于经典 Docker `manifest.json`/`repositories` 以及 Moby/containerd 导出的单目标平台 hybrid（`oci-layout` + `index.json` + `manifest.json` + content-addressed blobs）。嵌套 index 与 exporter 添加的 attestation/referrer 必须被显式核算；不支持的 metadata 在 load 前拒绝。

从源码 checkout 根目录配置运行环境后运行 `deploy/up.sh`。入口依次解析 core、WebUI、proxy 三套 Compose 配置为私有临时 JSON，运行 `deploy/check-ports.sh` 与 `deploy/provision-networks.sh`，再通过 `run_owned` 运行 `deploy/check-sandbox-dns.sh`、`deploy/firewall/docker-user-rules.sh` 与 `deploy/firewall/check.sh`，然后用已检查的快照启动 core 和 WebUI，最后启动 proxy。core/WebUI 的项目目录是源码根；proxy 的项目目录是 overlay，以保持 `../proxy` 构建上下文。启动时覆盖 `COMPOSE_REMOVE_ORPHANS=false` 和 `COMPOSE_PROFILES=`，避免拆除共享项目中的兄弟栈或激活 cleanup。nginx 在配置校验时解析 `open-webui:8080`、`computer-use-server:8081` 和 `documentserver:80`，因此三个上游必须先存在。nginx 只在启动时解析这三个名字，不会跟随后端重建更新地址；旧地址可能已分配给另一个服务（#612）。因此 proxy 启动后，入口再用同一项目、项目目录、env 文件和快照运行 `docker compose … restart proxy messages-ingress`（the Messages ingress also resolves `open-webui` only at start），成功时在 stderr 输出 `deploy: proxy and messages-ingress restarted for <proxy 快照>`，失败时输出 `deploy: proxy and messages-ingress restart failed for <proxy 快照>` 并以非零退出。每次运行都会重启，包括没有变更的重跑，`${OCU_PROXY_PORT}` 与 `${OCU_OFFICE_PROXY_PORT}` 会短暂中断。在 `up.sh` 之外重建的后端（例如手动 `docker compose up`，或通过恢复激活早于此变更的保留版本，其自带的 `up.sh` 不重启 proxy）需要在已加载 `runtime.env` 的部署 shell 中从源码根手动运行 `docker compose -p "$COMPOSE_PROJECT_NAME" --project-directory deploy/production-like-test --env-file /dev/null -f deploy/production-like-test/compose.proxy.yml restart proxy messages-ingress`。A release without the ingress (older than #655) has no `messages-ingress` service, so the same command fails with "no such service" and restarts nothing; for such a release end it with `restart proxy` alone.入口监督配置解析、快照冻结、检查、建网、DNS 预检、防火墙安装和启动子进程；TERM/INT/HUP 会结束所属进程组并删除私有临时文件，但不会 `down`、删除卷、迁移网络、刷新共享防火墙链或修改现存 sandbox。

- 只有 proxy 发布两个 TCP 端口：`${OCU_PROXY_PORT}:8082` 和 `${OCU_OFFICE_PROXY_PORT}:8083`。WebUI、Computer Use、DocumentServer、PostgreSQL、initializer 和维护服务不得发布宿主机端口，即使只绑定 loopback 也不允许。
- 应用服务仅连接由 `${OCU_PRIVATE_NETWORK}` 命名、`${OCU_PRIVATE_SUBNET}` 与 `${OCU_PRIVATE_GATEWAY}` 定址的 control-plane bridge（single exception: `messages-ingress`, see [Sandbox Messages ingress](#sandbox-messages-ingress-655)）；proxy 通过同一 bridge 的 Docker DNS 找到应用。显式 `network_mode`（包括 `bridge`）不得代替该命名网。
- `${OCU_SANDBOX_NETWORK}` 是部署入口单独创建或校验的非 internal bridge，具有 `${OCU_SANDBOX_SUBNET}` 和 `${OCU_SANDBOX_GATEWAY}`。Compose 服务不加入此网络，`messages-ingress` 除外（control-plane plus this bridge at `${OCU_SANDBOX_MESSAGES_ADDRESS}`, no published port; `deploy/check-ports.sh` rejects every other service on it）。OCU 将 CDP/ttyd 动态端口只绑定到该 gateway，且原生网络策略只允许 sandbox 连接这张 bridge。
- Open WebUI 必须设置 `ENABLE_OCU_WORKSPACE=true` 和 `OCU_INTERNAL_URL=http://computer-use-server:8081`；`ORCHESTRATOR_URL` 不是客户端别名。
- 运行环境必须显式提供 `OCU_RELEASE_MANIFEST`、`OCU_PRIVATE_NETWORK`、`OCU_PRIVATE_SUBNET`、`OCU_PRIVATE_GATEWAY`、`OCU_SANDBOX_NETWORK`、`OCU_SANDBOX_SUBNET`、`OCU_SANDBOX_GATEWAY`、`OCU_SANDBOX_EGRESS_ALLOW`、`OCU_SANDBOX_MESSAGES_ADDRESS`、`OCU_SANDBOX_DNS`、`OCU_PROXY_PORT`、`OCU_PROXY_IMAGE`、`OCU_INTERNAL_TOKEN`、`OCU_WEBUI_ORIGIN`、`OCU_WEBUI_AUTH_URL` 和 `PUBLIC_BASE_URL`，以及上述 Office 设置、既有应用和 provider 的必要变量。`OCU_SANDBOX_EGRESS_ALLOW` 未设置是配置错误；the firewall installs an explicit empty value as deny-all, but `deploy/up.sh` and `bootstrap-test.sh` refuse any allowlist that does not cover `OCU_SANDBOX_MESSAGES_ADDRESS`, so a deployable allowlist is never empty.`OCU_SANDBOX_DNS` 未设置是配置错误；显式空值通过容器本地 `127.0.0.11` 覆盖关闭外部 DNS 转发，而不是继承宿主机 nameserver。不要将 token 写在命令行、日志或仓库文件中。`scripts/bootstrap-test.sh` 要求 `OCU_RELEASE_MANIFEST`、匹配 checkout 的完整 `SOURCE_SHA`、`OCU_WEBUI_ORIGIN` 及上述显式 Office 输入；七张镜像引用必须与清单一致，或从清单安全派生。它写入上述拓扑/鉴权变量（`OCU_SANDBOX_MESSAGES_ADDRESS` defaults to `172.31.0.254` and is validated before anything is written）、`PUBLIC_BASE_URL=${OCU_WEBUI_ORIGIN}/ocu`、内部 `OCU_WEBUI_AUTH_URL=http://open-webui:8080/api/v1/ocu/auth`、共享内部 token、`OCU_PUBLIC_PREFIX=/ocu` 与 `OCU_SANDBOX_NO_AUTOSTART=1`。

`deploy/check-ports.sh` 的输入是 **完整的** `docker compose config --format json` 输出集合，不能用原始 YAML 代替；`expose` 不发布端口。bridge 已存在但 driver、internal 模式、subnet 或 gateway 不匹配时入口拒绝启动，不删除、替换、断开网络或现存 sandbox。防火墙安装绑定权威 sandbox 网桥入口接口，而不是源地址；同一宿主机只维护一份 owned 策略。安装器与检查器通过 `OCU_SANDBOX_EGRESS_LOCK`（默认 `/run/ocu-sandbox-egress/ocu-sandbox-egress.lock`）串行化合作进程；该路径必须位于当前 euid 拥有、非符号链接、非 group/world-writable 的目录中。DNS 解析、真实镜像构建、Compose 合并、内核数据包路径和引擎端口矩阵的实际验收留给 #36。

部署完成后不要把 overlay smoke 接到 `deploy/up.sh`。在同一已配置的部署 shell 中运行 `deploy/smoke.sh`：使用与 `up.sh` 相同的存储配置；smoke 从 `OCU_RELEASE_MANIFEST` 自动派生 Compose 所需的 release-font 路径，覆盖过期的继承值，无需额外导出。另需显式 `OCU_SMOKE_CHAT_ID`、`OCU_SMOKE_SANDBOX_ID`、`OCU_SMOKE_EXCLUSIVE=1`、`OCU_SMOKE_OWNER_TOKEN`（只进进程环境，不得出现在 argv/日志）、IPv4 字面量 `OCU_SMOKE_FORMER_URL`、`OCU_SMOKE_DOCUMENTSERVER_URL`、`OCU_SMOKE_EGRESS_URL` 与 `OCU_SMOKE_HOST_LAN_IPV4`。命令核验三套 Compose `ps --all` 清单（仅 proxy 发布 TCP `${OCU_PROXY_PORT}:8082` 与 `${OCU_OFFICE_PROXY_PORT}:8083`，各需 IPv4 wildcard，可有一个 IPv6 twin，不按地址族重复计端口；`computer-use-server`/`open-webui`/`proxy`/`messages-ingress`/`postgres`/`retention-guard`/`documentserver` 必须在跑，且不受编辑开关影响；oneshot 可缺席但其 publication 仍检查；运行中的 `cleanup` 失败）、前 OCU 与直接 DocumentServer 入口各自的 `ECONNREFUSED`、宿主机对 control 字面量的 HTTP 存活、sandbox 内允许列表 HTTP 2xx/3xx、the two Messages ingress probes (see [Sandbox Messages ingress](#sandbox-messages-ingress-655); `OCU_SANDBOX_MESSAGES_ADDRESS` is required)，然后才接受 sandbox 对 OCU:8081/WebUI:8080/proxy:8082 与宿主机 LAN proxy 的 curl 连接阶段超时（exit 28 且无 TCP 连接）。空允许列表是合法 deny-all，但不能完成本 smoke，以非零前提失败。终端只在独占、无既有 ttyd/tmux 的 smoke sandbox 上经 `dangerous_mode=false` 的 start-ttyd 与 `tty` WebSocket 观察产品 pane/前台 Bash；清理失败即失败。退出码：0 全部断言与 owned cleanup 完成；1 断言失败；2 前提/用法；129/130/143 为 HUP/INT/TERM。本地 fake/native 证据不是真实引擎验收；数据包、ttyd 镜像与宿主防火墙证明留给 #36。

`OCU_SMOKE_DOCUMENTSERVER_URL` 指向应当关闭的直接入口，不是内部 control-plane 地址或第二个 proxy 端口。它与 `OCU_SMOKE_FORMER_URL` 接受 IPv4 `host:port` 或带显式端口的 `http://IPv4:port/...`；端口范围为1–65535。缺失或格式错误以退出码2命名输入。两个入口分别检查：连接成功、超时或非 `ECONNREFUSED` 错误均以退出码1命名对应断言，不能用另一个入口的拒绝代替。

## Sandbox Messages ingress (#655)

`messages-ingress` (container `ocu-test-messages-ingress`, label `ocu.role=messages-ingress`) is the only endpoint sandboxes can newly reach. It runs the proxy image with the static `deploy/proxy/messages-ingress.conf`; Compose overrides the entrypoint, so `render.py` and `OCU_INTERNAL_TOKEN` are never involved and the container holds no secret. It joins the control-plane network and `${OCU_SANDBOX_NETWORK}` at `${OCU_SANDBOX_MESSAGES_ADDRESS}`, listens on port 8080 and publishes nothing.

| Request | Answer |
| --- | --- |
| `POST /api/v1/messages`, `POST /api/v1/messages/count_tokens`, any query | Forwarded to `http://open-webui:8080` with the same URI; only `Authorization`, `x-api-key`, `Content-Type`, `Accept`, `anthropic-version`, `anthropic-beta` and `User-Agent` are forwarded; bodies above 32 MiB get 413 locally; responses stream unbuffered; 600 s read/send timeouts |
| `GET /healthz` | 200 locally, used by the container healthcheck and the deployment smoke |
| Anything else, including dot segments, doubled slashes and percent-encoded paths | 404 locally |

Open WebUI authenticates every forwarded request; the ingress adds no identity. Claude Code inside a sandbox uses `ANTHROPIC_BASE_URL=http://<OCU_SANDBOX_MESSAGES_ADDRESS>:8080/api` with the user's own WebUI token.

`OCU_SANDBOX_MESSAGES_ADDRESS` must be an IPv4 address inside `OCU_SANDBOX_SUBNET`, not its network, broadcast or gateway address, and covered by an `OCU_SANDBOX_EGRESS_ALLOW` entry (use its `/32`). `python3 deploy/firewall/policy.py messages-address` checks this; `deploy/up.sh` runs it before `check-ports.sh` and any Compose start, and `bootstrap-test.sh` runs it before writing `runtime.env`. Choose the highest usable host of the subnet (`.254` in a /24): Docker IPAM gives sandboxes the lowest free address first, so a host with about 20 users never reaches it. If a sandbox holds the address anyway, Compose fails to start the ingress. The address is not reserved with `--ip-range`, which would require recreating the sandbox network with every sandbox stopped; revisit if a host runs more than about 200 sandboxes.

`deploy/check_sandbox_dns.py` skips the container labelled `ocu.role=messages-ingress`, because it resolves `open-webui` through Docker DNS; every other container on the bridge keeps the sandbox DNS check. Set `OCU_SMOKE_EGRESS_URL=http://<OCU_SANDBOX_MESSAGES_ADDRESS>:8080/healthz` for the smoke's allowlist probe when no other egress is allowed. The smoke also requires `messages-ingress` to run, expects `POST /api/v1/messages/count_tokens` without credentials to return Open WebUI's 401 and `GET /api/v1/chats/` to return the ingress's own 404.

## Upgrading an existing deployment

An upgrade keeps the deployment's data and configuration where they are: `$DEPLOY_ROOT/config/`, `$DEPLOY_ROOT/data/` and the Docker volumes. Each release gets its own install root, because `release.py import` refuses an existing one. `OCU_RELEASE_MANIFEST` in `runtime.env` selects the running release. `deploy/up.sh` and `write-deployed-version.sh` use the `source` checkout next to that manifest. On a fresh install the release root is `$DEPLOY_ROOT` itself.

Run steps 2 to 7 as root on the deployment host, with `DEPLOY_ROOT` exported as the deployment root (default `/opt/ai-workbench-test/open-computer-use`).

1. Build the release from committed OCU and WebUI checkouts, as in the quick start above.
2. Transfer the delivery directory to the deployment host. Compare `sha256sum release.json` on both hosts, then check the bundle and archives against it from the current release's source checkout:

   ```bash
   python3 deploy/release.py verify --inventory <delivery>/release.json --mode delivery --delivery <delivery>
   ```

3. Import into a new root next to the current one:

   ```bash
   new_root=/opt/ai-workbench-test/release-<ocu-short-sha>
   python3 deploy/release.py import --delivery <delivery> --install-root "$new_root"
   ```

   `import` first checks every archive in the delivery against `release.json`. A role whose image is already loaded, with an identity and platform `release.json` accepts, is skipped: it is neither copied nor loaded, so usually only the changed roles are. The source bundle, the fonts and the other roles' archives are copied into a private staging directory and loaded from there. `docker load` unpacks a new image to about three times its archive size. Before copying anything, `import` checks free space. The staging filesystem (the install root's parent) needs the archives to load, plus twice the source bundle and fonts, plus 1 GiB. Docker's data root (`DockerRootDir` in `docker info`) needs three times the archives to load plus 1 GiB. When both are on one filesystem it needs the sum, with the 1 GiB counted once. If either is short, `import` refuses with the path and the needed and available bytes, and stages nothing. Archives count whole even when the new image shares layers with an old one, and there is no override: free space or delete old images, then retry. The check assumes containerd's own root, if the image store uses one, is on the data root's filesystem, as the default `/var/lib/containerd` usually is. Keep the build host's BuildKit cache between releases: without it every `open-webui` layer is rebuilt and none is shared with the previous release's image, so the whole image is unpacked again. Step 3 runs the current release's `release.py`, so the first upgrade from a release without these checks still copies and loads every role. `import` deletes its staged copies when it finishes. Keep the delivery of every release you may later restore or activate: `recovery.py restore` and `activate` take it as `--retained-delivery`, and the recovery set holds no image archives ([BACKUP-RESTORE.md](../BACKUP-RESTORE.md)).
4. If you may need the newer release's database changes undone, first take a cold backup with the current release's `recovery.py backup` ([BACKUP-RESTORE.md](../BACKUP-RESTORE.md)). It must run before this step: `backup` checks the running containers against the release that `runtime.env` names, and refuses once `runtime.env` names the new release. It leaves the writers stopped until step 5's `up.sh`. Then point `runtime.env` at the new root with the new release's own `recovery.py`:

   ```bash
   python3 "$new_root/source/deploy/recovery.py" retarget \
       --runtime-file "$DEPLOY_ROOT/config/runtime.env" --release-root "$new_root"
   ```

   It checks the new root's `release.json` and `source` checkout, then rewrites only `SOURCE_SHA`, `WEBUI_SOURCE_SHA`, `OCU_RELEASE_MANIFEST` and the seven image variables. Every other line stays byte for byte. The previous file is kept as `runtime.env.before-<first 12 hex digits of the new OCU commit>`, mode `0600`. The command prints one line per identity key, `same` or `changed` with the release value, then the backup path. It never prints any other key. It refuses, and changes nothing, if `runtime.env` contains a comment or blank line (the rewrite re-renders the file, so remove them first), an identity key is missing, the backup name already exists, or the release does not verify.
5. If `runtime.env` has no `OCU_SANDBOX_MESSAGES_ADDRESS` (a release before #655), add it and append its `/32` to `OCU_SANDBOX_EGRESS_ALLOW`, editing those two lines by name without printing the file; `up.sh` refuses to start until both are set ([Sandbox Messages ingress](#sandbox-messages-ingress-655)). Load `runtime.env` and the provider key as in the [#590 preamble](#re-running-the-initializer-and-removing-the-wrapper-model-590). It changes into the new root's `source`. Then run `deploy/up.sh`.
6. Run `deploy/production-like-test/scripts/write-deployed-version.sh`. `$DEPLOY_ROOT/DEPLOYED_VERSION.md` now names the new OCU and WebUI commits.
7. Check: `up.sh` stderr shows `deploy: proxy and messages-ingress restarted for …`. `docker ps` shows `computer-use-server`, `open-webui`, `proxy`, `messages-ingress`, `postgres`, `retention-guard` and `documentserver` running. `${OCU_PROXY_PORT}` and `${OCU_OFFICE_PROXY_PORT}` answer through the proxy. If the release needs the initializer re-run, continue with the #590 section.

Rollback: the backup named after the release you upgraded to holds the previous configuration. Copy it back and repeat step 5; the preamble now changes into the previous root's `source`. When the previous release predates #655, skip step 5's first sentence: that release has no ingress, and the restored `runtime.env` predates the step 5 edit, so it correctly has neither `OCU_SANDBOX_MESSAGES_ADDRESS` nor the extra allowlist entry. Then run the newer root's version writer by absolute path, `"$new_root/source/deploy/production-like-test/scripts/write-deployed-version.sh"`, instead of step 6: it reads the manifest from `runtime.env` and the `source` next to it, while the writer of a release older than #626 reads `$DEPLOY_ROOT/source`.

```bash
install -m 0600 "$DEPLOY_ROOT/config/runtime.env.before-<new-12-hex>" "$DEPLOY_ROOT/config/runtime.env"
```

If the previous release predates #612, its `up.sh` does not restart the proxy. Run the manual `docker compose … restart proxy` from [部署入口与端口边界](#部署入口与端口边界) as well, ending with `restart proxy` alone: a release that old also predates #655, so `restart proxy messages-ingress` fails with "no such service" and does not restart the proxy. If the previous release predates #655, remove the ingress before step 5 with `docker rm -f ocu-test-messages-ingress`: `COMPOSE_REMOVE_ORPHANS=false` keeps the orphan running, and the older release's sandbox DNS check rejects it. Rollback restores configuration and images, not data: database changes made by the newer release stay. Undoing them needs the cold backup from step 4. To upgrade to the same release again later, move the `runtime.env.before-<new-12-hex>` file aside first, because `retarget` refuses an existing backup name.

Old release roots are not removed automatically. Delete a previous root by hand only after the new release has passed its checks and you will not roll back to it. Never delete the root that `OCU_RELEASE_MANIFEST` names, and never delete `$DEPLOY_ROOT`, which also holds the data.

## Re-running the initializer and removing the wrapper model (#590)

The Computer Use tool reaches every model through the global `computer_use_filter`; no model carries it any more, and `BYPASS_MODEL_ACCESS_CONTROL=true` on `open-webui` makes every provider model visible to every user. A deployment initialized before #590 still has the old filter, which acts only when the request already names the tool, and a wrapper row `<PRIMARY_CHAT_MODEL> (Computer Use)` that names it. The filter source lives in the WebUI database. Only the one-shot `open-webui-init` service writes it, and `init.sh` exits without changes while the `.computer-use-initialized` marker exists in the `open-webui-data` volume. Upgrade in this order, as root, from the selected release's source checkout (`DEPLOY_ROOT` defaults to `/opt/ai-workbench-test/open-computer-use`):

```bash
set -a; . "$DEPLOY_ROOT/config/runtime.env"; set +a
cd "$(dirname "$OCU_RELEASE_MANIFEST")/source"
# The provider key is not in runtime.env. Load it from the provider env file as
# a normal deploy shell does: DMX_ENV_FILE in runtime.env names it on recovered
# targets; otherwise it is the file bootstrap-test.sh read.
provider_env=${DMX_ENV_FILE:-/home/ubuntu/.config/ocu-test/dmxapi.env}
set -a; . "$provider_env"; set +a
# Never print this environment.
```

The WebUI overlay interpolates `${DMXAPI_API_KEY}` without a required-value check, so a shell without it starts `open-webui` with an empty provider key.

1. Deploy the new release as described in [Upgrading an existing deployment](#upgrading-an-existing-deployment). `deploy/up.sh` starts the proxy last and then restarts it, so nginx resolves `open-webui`, `computer-use-server` and `documentserver` to the containers this run recreated. `BYPASS_MODEL_ACCESS_CONTROL` is now live; `open-webui-init` still finds the marker and skips.
2. Delete the marker, then re-run `open-webui-init`:

   ```bash
   docker volume inspect "${COMPOSE_PROJECT_NAME}_open-webui-data" >/dev/null
   docker run --rm --network none --user 0:0 \
       -v "${COMPOSE_PROJECT_NAME}_open-webui-data:/data" \
       --entrypoint rm "$OPENWEBUI_IMAGE" /data/.computer-use-initialized
   deploy/up.sh
   docker inspect -f '{{.State.StartedAt}} exit={{.State.ExitCode}}' ocu-test-open-webui-init
   docker logs ocu-test-open-webui-init 2>&1 | tail -n 3   # expect "[init] Done!"
   ```

   `up -d` starts the exited one-shot again; a `StartedAt` from this run and exit code 0 confirm it. If the container did not run again, start that one service explicitly:

   ```bash
   COMPOSE_REMOVE_ORPHANS=false COMPOSE_PROFILES= docker compose -p "$COMPOSE_PROJECT_NAME" \
       --project-directory "$PWD" --env-file /dev/null \
       -f docker-compose.webui.yml -f deploy/production-like-test/compose.webui.override.yml \
       up -d --no-build --pull never --no-deps --force-recreate open-webui-init
   ```

3. Remove the wrapper row with a one-off container on the private network. It uses the same `ADMIN_EMAIL` and `ADMIN_PASSWORD` as `open-webui-init`, passed by name, and the same `openwebui/` source mount:

   ```bash
   docker run --rm --network "$OCU_PRIVATE_NETWORK" --user 0:0 \
       --security-opt no-new-privileges:true \
       -e WEBUI_URL=http://open-webui:8080 -e ADMIN_EMAIL -e ADMIN_PASSWORD \
       -v "$PWD/openwebui:/app/source:ro" \
       -v "$PWD/deploy/production-like-test/init/remove-wrapper-model.py:/bootstrap/remove-wrapper-model.py:ro" \
       --entrypoint python3 "$OPENWEBUI_IMAGE" /bootstrap/remove-wrapper-model.py
   ```

   The script refuses, exit 1, until the stored `computer_use_filter` content equals `/app/source/functions/computer_link_filter.py` byte for byte, which is what step 2 writes. It then lists rows from two admin endpoints and merges them by id. `GET /api/v1/models/base` is needed because the old initializer gave the wrapper the raw provider model's id, so WebUI stored it as a base-model override (`base_model_id` empty), and `GET /api/v1/models/export` lists only rows derived from another model. From that list the script deletes, with `POST /api/v1/models/model/delete`, only rows whose name ends with ` (Computer Use)` and whose `meta.toolIds` contains `ai_computer_use`. Deleting a row also revokes its access grants. A second run prints `nothing to remove` and exits 0. It never prints the password or the session token.

The wrapper's id equals the raw provider model id, so its row only overrode that model. After step 3 the id resolves to the raw provider model, and existing wrapper chats continue on it with the tool attached by the filter.

Re-running `open-webui-init` has side effects:

- `DEFAULT_MODELS` is written again as `PRIMARY_CHAT_MODEL`, and `DEFAULT_MODEL_PARAMS` gets `function_calling: native` and `stream_response: true` again.
- The tool and filter sources are replaced with the shipped files, and their valves are re-seeded from the environment (`ORCHESTRATOR_URL`, `MCP_API_KEY`, `DEBUG_LOGGING=false`; `ARCHIVE_BUTTON=on`, `INJECT_SYSTEM_PROMPT=true`). Valve edits made in the admin UI are lost.
- The tool is set back to public-read, and the filter back to active and global.
- Init signs in with `ADMIN_PASSWORD` from the runtime configuration. Signup is disabled (`ENABLE_SIGNUP=false`), so if the admin password was changed in the UI, init fails without writing the marker. Set the admin password back to the runtime value before step 2; the removal script has the same requirement.

## Tearing down and rebuilding the test deployment

Use this only when a deployment must start from an empty database, for example to prove a fresh bootstrap. It destroys every user, chat, workspace volume and Office version of the selected Compose project. Take a backup first ([BACKUP-RESTORE.md](../BACKUP-RESTORE.md)) if anything must survive. Run every step as root. The networks named by `OCU_PRIVATE_NETWORK` and `OCU_SANDBOX_NETWORK` and the `DOCKER-USER` firewall rules are shared host state: they stay, and the next `deploy/up.sh` reuses them. Below, `PROJECT` is the Compose project (`ocu-test`) and `DEPLOY_ROOT` the current install root.

1. Import the new release next to the current root. `import` refuses an existing install root, so use a new path:

   ```bash
   python3 deploy/release.py import --delivery <release-dir> --install-root "$DEPLOY_ROOT.next"
   ```

2. Note the non-secret inputs that `bootstrap-test.sh` needs and the current `runtime.env` already holds: `OCU_WEBUI_ORIGIN`, `OCU_OFFICE_DOCSERVER_ORIGIN`, `ENABLE_OCU_OFFICE_EDIT`, `OCU_SANDBOX_EGRESS_ALLOW`, `OCU_SANDBOX_MESSAGES_ADDRESS`, `OCU_SANDBOX_DNS`, `OCU_PROXY_PORT`, `OCU_OFFICE_PROXY_PORT`, `OCU_PRIVATE_NETWORK`, `OCU_PRIVATE_SUBNET`, `OCU_PRIVATE_GATEWAY`, `OCU_SANDBOX_NETWORK`, `OCU_SANDBOX_SUBNET`, `OCU_SANDBOX_GATEWAY`, `OPENWEBUI_VERSION`. Read them by name; do not print the file, which also holds secrets. Secrets are not carried over: bootstrap generates new ones.
3. List the deployment's containers and volumes, and keep the exact names:

   ```bash
   docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}'
   docker ps -a --filter label=managed-by=mcp-computer-use-orchestrator --format '{{.Names}} {{.Label "chat-id"}}'
   docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}'
   docker volume ls --format '{{.Name}}' | grep -E '^chat-[0-9a-f-]{36}-workspace$'
   ```

   Sandboxes (`owui-chat-<chat>`) and workspace volumes (`chat-<chat>-workspace`) are not Compose resources. Keep only those whose chat id has a directory under `$DEPLOY_ROOT/data/chat/`. On a host that runs another deployment, the rest belong to it.
4. Remove exactly those names, without patterns: `docker rm -f <containers>`, then `docker volume rm <volumes>`. Do not use `docker compose down -v`: it misses the sandboxes and workspace volumes, and it acts on whatever project files the shell points at.
5. Move the old root and the admin credential file aside. `bootstrap-test.sh` refuses to overwrite either. Then move the new root into place, and carry the operator fonts over when they live under the root (`OCU_OFFICE_FONTS_DIR` defaults to `$DEPLOY_ROOT/data/office-fonts`):

   ```bash
   mv "$DEPLOY_ROOT" "$DEPLOY_ROOT.before-rebuild"
   mv /root/ocu-test-openwebui-admin-credentials.txt /root/ocu-test-openwebui-admin-credentials.txt.before-rebuild
   mv "$DEPLOY_ROOT.next" "$DEPLOY_ROOT"
   mkdir -p "$DEPLOY_ROOT/data/office-fonts"
   cp -a "$DEPLOY_ROOT.before-rebuild/data/office-fonts/." "$DEPLOY_ROOT/data/office-fonts/"
   ```

6. Bootstrap from `$DEPLOY_ROOT/source` with the inputs from step 2 passed by name, in a clean environment so nothing stale leaks in:

   ```bash
   cd "$DEPLOY_ROOT/source"
   env -i PATH="$PATH" HOME=/root \
       SOURCE_SHA="$(git rev-parse HEAD)" OCU_RELEASE_MANIFEST="$DEPLOY_ROOT/release.json" \
       OCU_OFFICE_FONTS_DIR="$DEPLOY_ROOT/data/office-fonts" <inputs from step 2> \
       deploy/production-like-test/scripts/bootstrap-test.sh > /dev/null
   ```

   It writes a new `config/runtime.env` and a new `/root/ocu-test-openwebui-admin-credentials.txt`, both root-only `0600`. Redirect its output: it is the generated configuration.
7. Load `runtime.env` and the provider key as in the #590 section above, then run `deploy/up.sh` and `deploy/production-like-test/scripts/write-deployed-version.sh`.
8. Check: `open-webui-init` exited 0 and its `[init]` log lines show the filter, tool and default model were written; `docker ps` shows `computer-use-server`, `open-webui`, `proxy`, `postgres`, `retention-guard` and `documentserver` running; the admin from the new credential file can sign in through `OCU_PROXY_PORT`. Users other than that admin must be created again. Delete `$DEPLOY_ROOT.before-rebuild` and the `.before-rebuild` credential file once the check passes.

## 已知边界

部署入口在建网之后、启动应用之前核对 sandbox DNS 并安装出站策略：IPv4 经 `DOCKER-USER` 与 `INPUT` 的接口挂钩，且 `FORWARD` 的第一条规则必须是无条件 `-j DOCKER-USER`；IPv6 经 `INPUT` 与 `FORWARD` 默认 DROP。允许列表只豁免本守卫，不绕过后续宿主机策略；control-plane 子网与 metadata `169.254.169.254/32` 即使被宽网段覆盖也仍 DROP。控制面发起的 CDP/ttyd 应答走 conntrack `REPLY`，sandbox 发起的 ORIGINAL 已建立流仍受当前允许列表约束。control-plane 网桥在首次 Compose 启动前可以不存在，安装器此时使用已校验的配置 CIDR；已存在的 control-plane 网仍按 #24 校验。DNS 预检拒绝继承宿主机 nameserver 或不兼容的受保护网桥容器 DNS，但不声称真实数据包隔离；该证据属于 #36。`computer-use-server` 仍挂载 Docker socket，是高权限可信组件。未经授权的直接 OCU 入口不得用作回滚方案。

## 正式离线生产建议

1. 将 proxy 置于内网 TLS、公司 SSO/身份源和 IP allowlist 后面，不公开 MCP 或动态 sandbox 端口。
2. 高风险环境采用独立 Docker host、VM 或 Kubernetes node；按风险模型评估 rootless Podman、gVisor 或 Kata Containers。
3. 对 sandbox 出站网络按业务需要做 allowlist；禁止访问 Docker API、metadata 服务、公司管理网及凭据系统。
4. 导入已校验的 `release.json` 与镜像归档；启动禁止 build/pull。断网后验证 WebUI、RAG 和 sandbox 工具，并同时检查全新与既有 WebUI 配置。

5. 对话、embedding、rerank 连接内网 endpoint；模型 Key 只注入 WebUI，绝不传入 OCU 或 sandbox。
6. 定期用 `deploy/recovery.py` 与 `deploy/BACKUP-RESTORE.md` 做冷备份和独立空目标恢复演练；监控 Docker data-root、PostgreSQL、WebUI volume 和 chat data 的磁盘用量。未跟踪的本地草稿不是该入口。
