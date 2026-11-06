# 松果 NAS 开关机 · AstrBot 插件

用**松果电子开机卡**的开放 API，在聊天里控制 NAS / 主机的开机、关机、重启、强制关机与状态查询。

> 设备名不固定：示例里出现的「电脑」只是作者自己的设备名，**你填你自己的**。
> 同样，插件不绑定任何品牌，飞牛、群晖、威联通、Windows 主机，只要接了松果开机卡都能用。

## 安装方式

- **插件市场**：AstrBot 面板 → 插件市场 → 搜索「松果 NAS 开关机」→ 安装并启用
- **手动安装**：下载本仓库 Release 里的 zip，或直接把仓库内容放进 `AstrBot/data/plugins/astrbot_plugin_songguo_power/`，然后重载插件

---

## 二、安装

1. 把 `astrbot_plugin_songguo_power` 整个目录放进 `AstrBot/data/plugins/`
2. 打开 AstrBot 面板 → 插件 → 找到「松果 NAS 开关机」→ **重载** → **启用**
3. 进入插件配置，依次填写：

| 配置 | 填什么 |
|---|---|
| 松果账号 | 你的松果电子**主账号**（子账号/共享账号没有 API 权限） |
| 松果密码 | 对应密码 |
| 设备名 | 松果控制台里那台设备的名字，**必须完全一致**；不确定就先发 `/nas 设备` 看列表 |
| 允许使用的用户ID | 谁能用这条指令，填你的用户 ID；留空则只有 AstrBot 管理员可用 |

4. 保存 → 重载插件 → 发 `/nas 状态` 验证

> 账号密码只从配置读取，代码中没有硬编码；日志中账号脱敏、密码不打印。

---

## 三、设备名怎么确定

这是最容易填错的一项。

1. 先把账号密码填好并启用插件
2. 在聊天里发 `/nas 设备`
3. 插件会列出该账号下所有设备和状态，例如：

```
设备列表：
· 书房的电脑　开机 ← 当前配置
· 客厅主机　关机
```

4. 把要控制的那台名字，原封不动填进 `设备名`（大小写、空格、标点都要一致）

如果提示「设备不存在（-3）」，几乎都是名字对不上。

---

## 四、二次确认怎么用

```
你：/nas 关机
我：确认关机请回复：/nas 确认　（90 秒内有效）
你：/nas 确认
我：关机指令已下发，设备会执行。
```

- 超过 90 秒未确认会自动作废
- 强制关机的提示里会额外标明「强制断电，可能损伤文件系统」
- 同一用户 8 秒内不能重复下发（防连点），可用 `cooldown_sec` 调整

---

## 五、返回值含义

**动作执行返回**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| -1 | 失败 |
| 2 | 设备不在线 |
| -2 | 账号或密码错误 |
| -3 | 设备不存在 |
| -4 | 数据范围错误 |

**设备列表里的 status**

| 码 | 含义 |
|---|---|
| 0 | 关机 |
| 1 | 开机 |
| 2 | 离线 |
| 3 | 在线 |

⚠️ `value=11` 查询状态返回的 `1/0` 是「开/关」，和动作成功的 `0` **语义不同**，插件已分开处理。

---

## 六、接口实现说明

- 控制：`POST https://songguoyun.topwd.top/Esp_Api_new.php`
- 列表：`POST https://songguoyun.topwd.top/Esp_Api_advance.php`
- 请求体：**整段 JSON 先 urlencode，再作为 body 发送**，`Content-Type: application/x-www-form-urlencoded`
- 响应：实测返回体**也是 urlencode 过的 JSON**（如 `%7B%22deviceslist%22...`），插件会先按原文解析，失败再解码重试

---

## 七、配置项一览

| 项 | 默认 | 说明 |
|---|---|---|
| `sgdz_account` | 空 | 松果主账号 |
| `sgdz_password` | 空 | 松果密码 |
| `device_name` | 空 | 设备名，须与控制台一致 |
| `allowed_users` | 空 | 使用白名单；留空则仅管理员 |
| `timeout` | 15 | 请求超时（秒） |
| `cooldown_sec` | 8 | 同一用户动作最小间隔 |
| `body_mode` | quote | quote=整段 JSON urlencode；data_field=再包一层 `data=`（接口改版时用） |
| `value_power_on` | 1 | 开机 |
| `value_power_off` | 0 | 关机 |
| `value_query` | 11 | 查询状态 |
| `value_reboot` | 25 | 重启 |
| `value_force_off` | 14 | 强制关机 |
| `monitor_enable` | false | 巡检开关 |
| `monitor_interval_min` | 10 | 巡检间隔（最小 3 分钟） |
| `monitor_umo` | 空 | 巡检推送目标，**留空＝自动**用你最近说话的会话 |

换别的型号开机卡时，只改那五个 `value_*` 即可，不用改代码。

---

## 八、安全设计

1. **白名单**：不在名单里的人一律拒绝；名单为空时仅管理员可用
2. **二次确认**：关机/重启/强制关机都要确认，90 秒过期
3. **频率限制**：默认 8 秒内不能重复下发
4. **日志脱敏**：账号 `ab***yz`，密码永不输出
5. **零第三方依赖**：只用标准库 `urllib`、`ssl`、`json`，网络请求放线程池，不阻塞事件循环

---

## 九、常见问题

**Q：一直提示「账号或密码错误（-2）」**
A：用了子账号或共享账号。必须松果主账号。

**Q：提示「设备不存在（-3）」**
A：设备名不一致。发 `/nas 设备` 复制准确名字。

**Q：提示「设备不在线（2）」**
A：开机卡本身掉线了，检查它有没有通电、有没有连上 WiFi。

**Q：能控制多台吗**
A：一个插件实例控制一台。多台可以装多份插件，改不同设备名。

**Q：会不会误触关机**
A：不会，关机类动作都必须 `/nas 确认`。

---

## 十、目录结构

```
astrbot_plugin_songguo_power/
├── main.py             # 指令、二次确认、接口调用、巡检
├── metadata.yaml       # 插件元信息
├── _conf_schema.json   # 配置项定义
└── README.md
```

---

## 十一、更新记录

- **v1.1.2** 作者信息更正为「轩宇（xuanyu）」，同步 LICENSE 与元数据
- **v1.1.1** 增加「官方资料」章节：各型号安装配网教程、开放 API 文档、音箱联动教程，以及型号与 value 的对应说明
- **v1.1.0** 文档改为通用开源写法（不假设设备名、不绑定品牌）；补充设备名确认流程与常见问题
- **v1.0.0** 首版：状态查询、开机、关机、重启、强制关机、设备列表、二次确认、白名单、巡检推送

---

---

## 十二、官方资料（松果电子）

> 安装、配网、型号差异都以官方文档为准。下面链接来自松果官方帮助页
> `https://songguoyun.topwd.top/product_help/instructions.php`，按型号分类。

### 设备安装与配网

| 型号 | 教程 |
|---|---|
| 经典款 / 经济款开机卡 | https://mp.weixin.qq.com/s/CLDn-PWkFnd2Cf1IeJYEYw |
| USB 款 | https://mp.weixin.qq.com/s/0xxYWpeMYI6UmeR0jYq9CA |
| 笔记本（手指机器人） | https://mp.weixin.qq.com/s/LLpa5UvabfOLwKuhbp8sKA |
| 笔记本睡眠唤醒 | https://mp.weixin.qq.com/s/KIw0MSq0CnCjrv2zN7NEzQ |
| 通用款手指机器人 | https://mp.weixin.qq.com/s/oD5ikMZCbBXh0AYxtkUqfA |
| 开关灯机器人 | https://mp.weixin.qq.com/s/52AMG6LbiR1EkV4Ep5QHNg |

### 开放 API 文档

- 松果开放 API 控制接口：https://mp.weixin.qq.com/s/nh_fX3HnZpn7xdUzpopOPg

### 智能音箱联动（可选）

- 小爱同学使用教程：https://mp.weixin.qq.com/s/gobw0-z8_1e3JLXioE7TfQ
- 小度控制 WiFi 开机卡：https://mp.weixin.qq.com/s/010ofqQVMMDn10Hix5x3lg

### 型号与 value 的对应关系

本插件默认按**标准款（经典款/经济款）**的 value 表实现：

| 动作 | value |
|---|---|
| 关机 | 0 |
| 开机 | 1 |
| 强制重启 | 2 |
| 查询状态 | 11 |
| 强制关机 | 14 |
| 重启 | 25 |

如果你用的是 USB 款、笔记本款、手指机器人等不同型号，先查上面对应教程里的指令表，
再用配置项里的 `value_power_on / value_power_off / value_query / value_reboot / value_force_off` 覆盖，**不用改代码**。

### 落地注意

- 开机卡必须先在官方 App / 公众号里完成配网，能正常开关机之后，插件才有意义
- 只能用**松果主账号**调用 API，子账号/共享账号会一直返回「账号或密码错误（-2）」
- 设备名要和官方控制台里显示的名字完全一致

---

## 十三、许可

MIT License。欢迎提 issue / PR，二改二发请保留原作者信息。

## 相关项目

- [小智设备桥接](https://github.com/xuanyu1937/astrbot_plugin_xiaozhi_bridge) —— 同一作者的 AstrBot 插件

## 作者

轩宇（GitHub: [xuanyu1937](https://github.com/xuanyu1937)）
