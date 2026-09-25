# 网络与隔离加固说明

## 部署入口与端口边界

从源码 checkout 根目录配置运行环境后运行 `deploy/up.sh`。入口依次解析 core、WebUI、proxy 三套 Compose 配置为私有临时 JSON，运行 `deploy/check-ports.sh` 与 `deploy/provision-networks.sh`，然后用已检查的快照启动 core 和 WebUI，最后启动 proxy。core/WebUI 的项目目录是源码根；proxy 的项目目录是 overlay，以保持 `../proxy` 构建上下文。启动时覆盖 `COMPOSE_REMOVE_ORPHANS=false` 和 `COMPOSE_PROFILES=`，避免拆除共享项目中的兄弟栈或激活 cleanup。nginx 在配置校验时解析 `open-webui:8080` 和 `computer-use-server:8081`，因此两个应用必须先存在。入口监督配置解析、快照冻结、检查、建网和启动子进程；TERM/INT/HUP 会结束所属进程组并删除私有临时文件，但不会 `down`、删除卷、迁移网络或修改现存 sandbox。

- 只有 proxy 将 `${OCU_PROXY_PORT}` 映射到容器的 TCP 8082。WebUI、Computer Use、PostgreSQL、initializer 和维护服务不得发布宿主机端口，即使只绑定 loopback 也不允许。
- 应用服务仅连接由 `${OCU_PRIVATE_NETWORK}` 命名、`${OCU_PRIVATE_SUBNET}` 与 `${OCU_PRIVATE_GATEWAY}` 定址的 control-plane bridge；proxy 通过同一 bridge 的 Docker DNS 找到应用。显式 `network_mode`（包括 `bridge`）不得代替该命名网。
- `${OCU_SANDBOX_NETWORK}` 是部署入口单独创建或校验的非 internal bridge，具有 `${OCU_SANDBOX_SUBNET}` 和 `${OCU_SANDBOX_GATEWAY}`。Compose 服务不加入此网络。OCU 将 CDP/ttyd 动态端口只绑定到该 gateway，且原生网络策略只允许 sandbox 连接这张 bridge。
- Open WebUI 必须设置 `ENABLE_OCU_WORKSPACE=true` 和 `OCU_INTERNAL_URL=http://computer-use-server:8081`；`ORCHESTRATOR_URL` 不是客户端别名。
- 运行环境必须显式提供 `OCU_PRIVATE_NETWORK`、`OCU_PRIVATE_SUBNET`、`OCU_PRIVATE_GATEWAY`、`OCU_SANDBOX_NETWORK`、`OCU_SANDBOX_SUBNET`、`OCU_SANDBOX_GATEWAY`、`OCU_PROXY_PORT`、`OCU_PROXY_IMAGE`、`OCU_INTERNAL_TOKEN`、`OCU_WEBUI_ORIGIN`、`OCU_WEBUI_AUTH_URL` 和 `PUBLIC_BASE_URL`，以及既有应用和 provider 的必要变量。不要将 token 写在命令行、日志或仓库文件中。现有 bootstrap 文件的变量清单与备份固定库存分别由 #26、#34 更新；在此前缺少新变量时部署入口拒绝启动。

`deploy/check-ports.sh` 的输入是 **完整的** `docker compose config --format json` 输出集合，不能用原始 YAML 代替；`expose` 不发布端口。bridge 已存在但 driver、internal 模式、subnet 或 gateway 不匹配时入口拒绝启动，不删除、替换、断开网络或现存 sandbox。DNS 解析、真实镜像构建、Compose 合并和引擎端口矩阵的实际验收留给 #36。

## 已知边界

当前拓扑并不宣称 sandbox 对 control plane 具备 L3 隔离。`DOCKER-USER` 防火墙规则和部署入口的对应检查由 #25 添加；在该检查交付前不能把 proxy-only 端口矩阵称为安全的 LAN 隔离。`computer-use-server` 仍挂载 Docker socket，是高权限可信组件。未经授权的直接 OCU 入口不得用作回滚方案。

## 正式离线生产建议

1. 将 proxy 置于内网 TLS、公司 SSO/身份源和 IP allowlist 后面，不公开 MCP 或动态 sandbox 端口。
2. 高风险环境采用独立 Docker host、VM 或 Kubernetes node；按风险模型评估 rootless Podman、gVisor 或 Kata Containers。
3. 对 sandbox 出站网络按业务需要做 allowlist；禁止访问 Docker API、metadata 服务、公司管理网及凭据系统。
4. 使用内部 Registry 和依赖镜像，导入已测试 image digest；断网后验证 WebUI、RAG 和 sandbox 工具。
5. 对话、embedding、rerank 连接内网 endpoint；模型 Key 只注入 WebUI，绝不传入 OCU 或 sandbox。
6. 定期备份和受控恢复演练；监控 Docker data-root、PostgreSQL、WebUI volume 和 chat data 的磁盘用量。
