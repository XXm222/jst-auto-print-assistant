# V0.5.21 后台部署清单

本版本使用 API schema 5，只接受匹配 schema 5 的 V0.5.24 客户端。schema 5 的精确回读新增必填布尔字段 `has_print_request` 和不泄露完整单号的 `waybill_fingerprint`；请求证据不得与 `has_print_action` 完成证据混用。后台与 Windows 客户端应协调切换；切换前先停止旧客户端并处理或等待现有租约结束。Bearer 变更会派生出新的唯一工作站主体，不要在仍有活动租约时轮换。

## 1. 创建低权限服务账号

```sh
sudo useradd --system --home-dir /var/lib/jst-print-api \
  --create-home --shell /usr/sbin/nologin jst-print-api
sudo install -d -o root -g jst-print-api -m 0750 /opt/jst-print-api
sudo install -d -o root -g jst-print-api -m 0750 /opt/jst-erp-bridge
```

若账号已存在，跳过 `useradd`。SQLite 和候选缓存由 systemd 的 `StateDirectory=jst-print-api` 在 `/var/lib/jst-print-api` 中创建。

## 2. 安装后台与 ERP bridge

把下列三个后台文件复制到 `/opt/jst-print-api`，属主设为 `root:jst-print-api`、模式设为 `0640`：

- `jst_print_api_server.py`
- `jst_lease_store.py`
- `jst_print_shadow_plan.py`

把经过确认的 ERP bridge 程序及其 Python 依赖复制到 `/opt/jst-erp-bridge`。目录应为 `root:jst-print-api 0750`；普通代码文件可设为 `0640`。服务账号只需要读取代码、导入模块和遍历目录，不应拥有修改程序的权限。

ERP bridge 的实际凭据文件位置因部署而异。逐一确认 JST 密钥、令牌或配置文件为 `root:jst-print-api 0640`，其父目录为 `0750`，且没有 other 读权限。不要把凭据复制到源码包、systemd unit 或日志中。可用以下方式验证服务账号确实能读、其他用户不能读：

```sh
sudo -u jst-print-api test -r /opt/jst-erp-bridge/实际凭据文件
namei -l /opt/jst-erp-bridge/实际凭据文件
```

## 3. 配置唯一 Bearer

生成一个 32—128 字符、仅含 ASCII 字母、数字、下划线或连字符的随机值，例如 `openssl rand -hex 32`。安全编辑 `/etc/jst-print-api.env`：

```text
JST_PRINT_API_TOKEN=在此填入随机值
```

然后限制权限：

```sh
sudo chown root:root /etc/jst-print-api.env
sudo chmod 0600 /etc/jst-print-api.env
```

同一个值只配置到这台固定电脑的受保护客户端配置中，不要在命令行、截图、工单或日志里粘贴真实值。

## 4. 安装并启动 systemd unit

```sh
sudo install -o root -g root -m 0644 jst-print-api.service \
  /etc/systemd/system/jst-print-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now jst-print-api.service
sudo systemctl restart jst-print-api.service
sudo systemctl --no-pager --full status jst-print-api.service
```

unit 默认显式使用 `Asia/Shanghai` 业务时区，把 planner 子进程限制为 2 个、HTTP I/O 超时设为 15 秒，并把 inspect 完成证明缓存 30 秒（最多 256 条）。planner 本身也显式生成北京时间查询边界，不依赖 Linux 主机默认时区。服务仅监听 `127.0.0.1:8766`；公网入口仍应由现有 HTTPS 反向代理提供，不能直接暴露明文端口。

## 5. 健康检查与协调切换

```sh
curl --fail --silent --show-error \
  http://127.0.0.1:8766/jst-print-api/health
sudo journalctl -u jst-print-api.service -n 100 --no-pager
```

健康响应应包含 `"ok": true` 和 `"api_schema_version": 5`。随后部署匹配 schema 5 的客户端并确认鉴权 ping 成功。schema 5 的 `lease/complete` 必须发送以下四种明确原因之一：

- `PRINTED`
- `TERMINAL`
- `OPERATOR_SKIPPED`
- `UNCERTAIN_ACTION`

不要让旧客户端继续连接：后台最低客户端版本为 V0.5.21。现有 SQLite 会在线增加 `completion_reason` 列，旧的 `COMPLETED` 行允许该值为空；部署前仍建议做正常的数据库备份。
