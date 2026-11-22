# wake_UP 自建前端

一个**会主动找你的 AI 聊天服务**。跑在 24 小时常亮的 Windows 上，
浏览器打开就能聊，它会按你说过的话自己安排下次来找你。

底层用的是 [KKarsyline/wake_UP](https://github.com/KKarsyline/wake_UP) 的 `deamo_wake` 调度内核。

## 能做什么

- 你说「我出门了」→ 自动建立外出情境，之后按节奏回来找你
- 你说「每隔十分钟来找我一次」→ 登记重复唤醒
- 你说「九点叫我」→ 设定闹钟，到点自己醒来
- 你说「晚安」→ 建立睡前情境，安静时不来打扰
- 主动消息会自己出现在聊天窗口里（橙色标注「主动唤醒」）

## 准备

1. **Python 3.11 或以上**，并且已装好 `deamo_wake`：
   ```sh
   cd D:\机\wake_UP-main
   python -m pip install -e .
   ```
2. 一个 **OpenAI 兼容的中转站**：地址（带 `/v1`）、API Key、模型 id。

## 安装

```sh
git clone https://github.com/liam12345693/wake-up-app.git
cd wake-up-app
python setup.py
```

`setup.py` 会引导你填中转站地址、key、模型 id，写进本机 `config.json`，
然后直接启动服务。

**中转站的地址和 key 只写进你自己电脑的 `config.json`，不经过任何聊天窗口。**

## 启动

以后每次开机，双击 `start.bat` 就行。

浏览器打开：

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8000` | 这台电脑自己 |
| `http://192.168.31.127:8000` | 同一个 Wi-Fi 下的手机 / 别的电脑 |
| `http://100.79.83.10:8000` | 通过 Tailscale，外网也能连 |

> 第一次从别的设备访问，Windows 防火墙可能拦一下，允许 Python 访问专用网络即可。

## 常用指令

在聊天框里直接说，或者点下面的快捷按钮：

| 你说的 | 内部动作 |
|---|---|
| 每隔十分钟来找我一次 | `set_repeat`（cadence_min=10） |
| 九点叫我 | `set_alarm`（at=ISO 时间） |
| 我出门了 | `start_scene`（scene_type=outing） |
| 回来了 | `end_scene` |
| 晚安 | `start_scene`（scene_type=bedtime） |
| 先安静一小时 | `snooze`（minutes=60） |

快捷按钮对应：10 分钟后找我 / 每 30 分钟 / 暂停 1 小时 / 清除闹钟 / 结束情境。

## 文件说明

| 文件 | 作用 |
|---|---|
| `server.py` | 后端服务：内嵌 wake_UP 引擎 + 调中转站模型 + 网页接口 |
| `index.html` | 聊天前端 |
| `setup.py` | 安装与配置向导 |
| `config.json` | 你的中转站配置（本地，不上传） |
| `chat-history.json` | 对话历史（本地，重启不丢） |
| `wake-state.sqlite3` | wake_UP 调度状态（情境、闹钟、回执） |

## 想让它 24 小时在线

用 Windows 任务计划程序：

- 触发器：**用户登录时**
- 操作：启动程序 `python`
- 参数：`server.py`
- 起始位置：你的安装目录

## 常见问题

**页面显示「配置不完整」**
编辑 `config.json`，填好 `api_base`、`api_key`、`main_model`，重启服务。

**模型不响应**
先确认中转站能调通。PowerShell 里试：
```sh
curl.exe -H "Authorization: Bearer 你的key" -H "Content-Type: application/json" `
  -d "{\"model\":\"你的模型id\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" `
  你的中转站地址/v1/chat/completions
```

**它不主动来找我**
- 确认 `config.json` 里模型可用
- 主动巡检默认只在 09:00–24:00（UTC+8）运行
- 打开页面右上角「状态」看当前活跃情境和下次唤醒时间

## 许可证

wake_UP 采用 PolyForm Noncommercial License 1.0.0，仅限非商业使用。
本仓库的接入代码同此许可。
