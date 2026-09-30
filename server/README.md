# 🔐 服务器端

本目录集中存放后台 API、订单租约与永久排除存储、只读订单规划、systemd 服务模板及部署说明。

| 文件 | 用途 |
| --- | --- |
| `jst_print_api_server.py` | Bearer 鉴权、候选规划、精确回读、租约协调及强制跳过接口 |
| `jst_lease_store.py` | SQLite 租约与 `operator_skips` 永久排除记录 |
| `jst_print_shadow_plan.py` | 通过独立 ERP bridge 只读规划及回读订单 |
| `jst-print-api.service` | 低权限 systemd 服务模板 |
| `jst-print-api.env.example` | 服务端 token 的环境配置模板 |
| `erp-bridge-report-retention.conf` | ERP bridge 报告保留配置模板 |

## 部署入口

1. 先阅读 [基础部署说明](DEPLOYMENT_V0.5.21.md) 与 [本次强制跳过迁移说明](../docs/force-skip-20260930.md)。
2. 独立部署 ERP bridge 及其依赖、凭据；本仓库不包含 bridge 源码。
3. 将本目录三个 Python 模块部署到 `/opt/jst-print-api/`。运行中的服务需要备份原代码与 SQLite 数据库，再协调客户端切换。
4. 根据 `jst-print-api.env.example` 创建受保护的 `/etc/jst-print-api.env`，设置 `JST_PRINT_API_TOKEN`。客户端配置使用同一 token。
5. 核对 `jst-print-api.service` 中的路径、服务账号及环境，安装服务并配置 HTTPS 反向代理。

模板服务监听 `127.0.0.1:8766`，健康检查：

```sh
curl --fail --silent --show-error \
  http://127.0.0.1:8766/jst-print-api/health
```

API schema 为 **5**。强制跳过接口为 `POST /jst-print-api/v1/order/force-skip`；永久排除记录位于租约 SQLite 数据库中。运行数据与生产凭据均不纳入仓库。

## 回归检查

完整测试入口位于相邻的客户端目录，从仓库根目录执行：

```sh
cd client
python -m unittest discover -s tests -p 'test_*.py'
```

[🖥️ 本地客户端](../client/README.md) · [📖 项目总览](../README.md)
