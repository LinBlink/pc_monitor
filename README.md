# PC Monitor — 掌机 WiFi 实时看电脑状态

在 Windows PC 上跑一个小服务，把 CPU / 内存 / 网络 / GPU / 游戏 FPS 画成一张
仪表盘，通过 WiFi 以 MJPEG 流推给 Miyoo Mini Plus（Onion OS）全屏显示。掌机会自己
扫局域网找出所有能监控的 PC，左右键切换，Y 键转屏（支持竖屏），自己的电量也会显示
在顶栏、低电量时震动。

一屏包含：CPU 总占用 / 温度 / 功耗 / 每个逻辑核心的占用与实时频率、游戏 FPS、
GPU 占用 / 温度 / 功耗 / 显存、CPU 占用前三的进程、GPU 占用前三的进程、网络实时
上下行与当日累计流量、内存。

**为什么这么设计**：掌机是 ARMv7 双核、没有 python/lua/编译器，但自带 `ffplay`。
所以让 PC 承担全部采集与绘图，掌机只解码一路 640×480 MJPEG——完全在 Cortex-A7
的能力范围内，也不需要交叉编译任何东西。

## 组成

| 文件 | 作用 |
|---|---|
| `server.py` | MJPEG 服务，帧生产线程 + HTTP 接口 |
| `metrics.py` | 采集 CPU / 内存 / 网络 / GPU / 进程 / 当日流量 |
| `rtss.py` | 从 RivaTuner 共享内存读游戏 FPS |
| `sensors.py` | 从 MSI Afterburner 共享内存读 CPU 温度 / 功耗 / 每核频率 |
| `render.py` | Pillow 绘制仪表盘（横/竖两套版式 + 旋转 + 180° 预旋转） |
| `preview.py` | 用假数据出图，改版式时看效果 |
| `make_icon.py` | 生成掌机启动器图标 |
| `deploy_device.py` | 把掌机端推送到 `/mnt/SDCARD/App/PCMonitor` |
| `device/` | 掌机端：`launch.sh` / `config.json` / `settings.cfg` |

## 依赖

```
python -m pip install psutil pillow paramiko
```

`paramiko` 只在部署到掌机时需要。

## 使用

1. **PC 端启动**：双击 `start.bat`，或 `python server.py`。
   启动时会打印地址：

   ```
   settings     : http://192.168.2.114:8765/settings
   handheld URL : http://192.168.2.114:8765/stream.mjpg
   ```

   浏览器打开 `settings` 那条即可调设置并实时预览（手机上也能开）。

2. **开机自启**（推荐，这样掌机随时打开都有画面）：

   ```
   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Install
   ```

   卸载用 `-Remove`。

3. **掌机端部署**（已经部署过就不用再跑）：

   ```
   python deploy_device.py                  # 覆盖全部文件
   python deploy_device.py --keep-settings  # 保留掌机上已改的 settings.cfg
   ```

4. **掌机上打开**：Onion 的 `Apps` 菜单里选 **PC Monitor**。

> ⚠️ **重新部署前先在掌机上退出本应用。** busybox 的 sh 会边跑边读脚本文件，
> 覆盖正在运行的 `launch.sh` 会让它执行错乱并留下一堆僵尸进程。

## 掌机上的操作

| 按键 | 作用 |
|---|---|
| **LEFT / RIGHT** | 切换设备（在扫到的 PC 之间循环） |
| **Y** | 转屏，4 档：横向 → 竖向 → 横向倒置 → 竖向（另一侧） |
| **MENU** | 退出 |

当前选的设备和朝向记在 `state.cfg` 里，下次打开还是这个视角。

### 屏幕上的设备条

画面顶栏就是设备切换条：**当前设备是填色的胶囊**，左右两侧列出相邻设备，两端有
`‹ ›` 提示可以左右键切换；只有一台时不显示箭头，装不下时多出来的显示成 `+N`。

设备列表只有掌机知道，所以它跟在流地址里传给 PC（`?devs=名字1,名字2&i=当前序号`），
由 PC 连同仪表盘一起画出来——掌机上不需要任何绘图代码。名字取自各台 PC 的主机名
（`/config.json` 的 `name`）。

后台扫描发现列表有变化时会主动重连一次，所以新开机的 PC 会自己出现在设备条里，
不用按任何键；重连时会尽量停在你原来看的那台上。

### 自动发现

启动时掌机先用上次记住的设备出画面，同时在后台扫本机 `/24` 网段：对每个地址
`nc -w 1` 试 `PC_PORT`，端口开着的再取一次 `/config.json`，能解析出 `name` 的才算
一台 PC Monitor，结果写进 `hosts.txt`（`IP|主机名`，主循环每轮重读）。32 路并发扫
完 254 个地址约 5–9 秒。

之所以是 TCP 连接扫描而不是广播：掌机的 `nc` 没有 `-u`，`ping` 也没有 `-b`，UDP
发现和广播探测都做不了。

扫描没有任何结果时保留原有的 `hosts.txt`，不会把已知设备清空。当前设备连不上时会
每 2 秒重试，并在上一轮扫描结束后重新扫一遍。

### 竖屏

朝向由掌机通过 `?orient=N`（0–3，顺时针 1/4 圈为一档）告诉 PC，**版式和旋转都在
PC 端完成**：0/2 用横版 640×480，1/3 用竖版 480×640 再旋转进 640×480 的画框。
竖版不是把横版硬转——它是单独一套自适应高度的版式，五块指标的走势曲线一条都不少。

服务端只渲染真正有人在看的朝向，切换朝向不会增加负担。

## 设置页面

浏览器打开 `http://<PC>:8765/settings`：

- **刷新速率** 1–30 fps，带 5 档预设，实时估算带宽占用
- **画质** JPEG 质量 40–95
- **预旋转 180°** 开关（掌机画面上下颠倒时关掉它）
- 一张信息卡显示掌机当前朝向（横向 / 竖向 / 倒置）
- 页脚带一路实时预览（始终是正着的，跟着掌机的横竖切换）

改动**立即生效并写入 `config.json`**，越界值会被拒绝（HTTP 400），配置不变。

**掌机会自动跟随，不用在掌机上改任何东西。** 机制是：改帧率时服务端递增一个
generation 计数并踢掉所有在线的流客户端；掌机的 `launch.sh` 循环重连时会先
`curl /config.json | jq -r .fps` 读当前帧率，再用它启动 ffplay。整个过程约 1–2 秒。

这样设计是因为 ffplay 的 `-framerate` 必须在启动时给定：如果两边不一致，播放时序
就会漂。让掌机每次连接都去 PC 取，就不存在"两处配置要手动对齐"的问题。

### 配置项

PC 端 `config.json`（`port` 只能在这里改，改完要重启服务）：

| 键 | 默认 | 说明 |
|---|---|---|
| `port` | 8765 | 监听端口 |
| `fps` | 8 | 帧率。约 33 KB/帧，8 fps ≈ 2.2 Mbps |
| `jpeg_quality` | 72 | JPEG 质量 |
| `rotate180` | true | 预旋转 180°，匹配 Miyoo 面板方向 |

掌机端 `/mnt/SDCARD/App/PCMonitor/` 下：

| 文件 | 作用 |
|---|---|
| `settings.cfg` | `PC_PORT`（扫描用的端口，两边必须一致）；`PC_HOST` 只是首次运行的种子地址；`STREAM_FPS` 是读不到 `/config.json` 时的兜底帧率；`BATT_EVERY_S` / `BATT_LOW_PCT` / `BATT_BUZZ_GAP_S` 控制电量上报与震动 |
| `hosts.txt` | 扫到的设备表，每行 `IP\|主机名`，自动维护 |
| `state.cfg` | 上次选的 `IDX` 和 `ORIENT` |
| `pcmonitor.log` | 本次运行的日志，每次启动清空 |
| `.pid` | 运行中的实例 PID，启动时用它结束上一个实例 |

PC 的 IP 变了不用管，扫描会重新找到它——只有换端口才需要同时改 `PC_PORT` 和 PC 的
`config.json`。

## CPU 温度、功耗、每核频率

这三项来自 **MSI Afterburner** 的共享内存 `MAHMSharedMemory`（就是 RTSS 的同伴，
装了 Afterburner 就有）。**Afterburner 没运行时温度和功耗显示"温度需 Afterburner"，
每核心只画占用条不画频率**，其余指标不受影响。

为什么绕这一圈：Windows 不把 CPU 温度暴露给普通程序——`psutil.sensors_temperatures()`
在 Windows 上返回 None，WMI 的 thermal zone 顶多是主板传感器，要自己读就得装内核
驱动。Afterburner 的硬件监控已经在读 CPU 自己的寄存器，把结果发布在共享内存里，所以
直接读它就能拿到真值，不用自己上驱动。

`sensors.py` 按名字取值（`CPU temperature` / `CPU power` / `CPU3 clock` …）而不是按
下标，所以换机器、传感器数量变了也不会错位。Afterburner 读不到的传感器会填
±FLT_MAX，这种值一律当作"没有"。

顺带一提，**总频率也优先用 Afterburner 的**：Windows 上 `psutil.cpu_freq()` 报的是
标称基频（这台机器是 1800 MHz），跟实际睿频到 4.3 GHz 差得远。

## 进程排行

- **CPU 前三**：`psutil` 全进程遍历，3 秒一次（遍历不便宜，所以不跟帧率同步）。
  占用按整机归一化，跳过 pid 0 的 System Idle Process。
- **GPU 前三**：读 `\GPU Engine(*)\Utilization Percentage` 性能计数器，也就是任务
  管理器 GPU 那一列的来源。按 pid 把各引擎（3D / copy / video）加起来，再用 psutil
  把 pid 换成进程名。用它而不是 `nvidia-smi pmon` 的原因是它跟显卡厂商无关，而且能
  算上图形负载而不只是计算负载；用 `Get-Counter` 循环而不是 `typeperf` 通配符，是
  因为后者只在启动时枚举一次实例，之后新起的进程永远不会出现。

## 当日流量

`net.day_down` / `net.day_up` 是本地零点以来的累计字节数，数值大了自动从 MB 换到
GB、TB。

操作系统并不提供"今天用了多少"这种计数，所以这里累加的是和实时速率同一份增量，并
写进 `traffic.json`（每 30 秒一次，避免频繁写盘）——有这个文件，重启服务不会丢当天
的数据。**只统计服务在运行的时间段**，服务没开的时候走的流量不算。跨过本地零点自动
归零。

## 掌机电量与震动

掌机每 60 秒把自己的电量报给 PC：`/battery?pct=57&charging=0`，PC 按**请求来源地址**
归属这条数据，所以多台掌机不会互相覆盖，超过 120 秒没有新上报就不再显示（避免关掉
的掌机在画面上留一个过期电量）。顶栏画一个电池图形：>30% 用普通字色，≤30% 转黄，
≤15% 转红，充电时转绿并加 ⚡。

电量来自 `/customer/app/axp_test`，它输出 `{"battery":100, "voltage":4155,
"charging":3}`（必须在它自己的目录里执行；`charging=3` 在这台固件上表示插着电）。
读不到时退回 Onion 的 batmon 维护的 `/tmp/percBat`，那里只有百分比没有充电状态。

**低电量震动**：不充电且电量 ≤ `BATT_LOW_PCT`（默认 15）时震一下，最短间隔
`BATT_BUZZ_GAP_S`（默认 600 秒）。马达挂在 gpio48 上（平时为高，拉低即震），和
Onion 的 keymon 用的是同一个引脚；**Onion 自己的 `.noVibration` 开关会被尊重**，
所以在系统里关了震动，这里也不会震。`BATT_LOW_PCT=0` 可以单独关掉震动。

## 游戏 FPS

FPS 来自 **RivaTuner Statistics Server (RTSS)** 的共享内存，也就是 MSI
Afterburner 附带的那个组件。**RTSS 没运行时 FPS 显示 `—` 并提示启动它**，其余指标
不受影响。想常驻就把 Afterburner / RTSS 设成开机启动。

`rtss.py` 读的是 `RTSSSharedMemoryV2`（签名是 `SSTR`，普通权限即可读，不在
`Global\` 命名空间）。FPS 由 `frames × 1000 / (time1 − time0)` 得出，条目超过 3 秒
没更新就视为已停止出帧。

**只认前台窗口所属进程。** RTSS 会钩住所有 Direct3D 程序——微信、桌面悬浮窗、
TrafficMonitor 都在列表里，直接取"最新条目"会显示成 `TrafficMonitor.exe 9 FPS`
这种毫无意义的数字。所以只匹配 `GetForegroundWindow()` 对应的 PID；切出游戏时
显示"无游戏 / 前台没有游戏画面"。

## HTTP 接口

| 路径 | 内容 |
|---|---|
| `/` `/settings` | 设置页（POST 同一路径提交表单） |
| `/stream.mjpg?orient=N&devs=a,b&i=K` | 裸 JPEG 连续流，给掌机的 ffplay。`orient` 0–3 决定版式与旋转（缺省 0）；`devs` 是掌机扫到的设备名，`i` 是当前序号，用来画顶栏的设备条（最多 8 台、单名 18 字符，超出截断） |
| `/preview.mjpg` | `multipart/x-mixed-replace`，给浏览器 |
| `/preview` | 只有预览的页面 |
| `/frame.jpg` | 当前单帧 |
| `/config.json` | 当前生效的设置 + `name`（主机名）。掌机每次连接前读它取帧率，发现阶段也靠 `name` 判断"这是不是一台 PC Monitor" |
| `/battery?pct=57&charging=0` | 掌机上报自己的电量。用 GET 是因为调用方是掌机上的 busybox curl，而且每分钟重复一次，越简单越好 |
| `/stats.json` | 原始快照，想自己做别的客户端就用这个 |

## 排查

- **掌机黑屏**：说明连不上任何 PC。看 `/mnt/SDCARD/App/PCMonitor/pcmonitor.log`，
  里面会记 `discover: swept …, N port(s) open`、`discover: found N device(s)`、
  `connect <ip> idx=i/n orient=o rate=r`、`unreachable <ip>` 和 ffplay 的退出码。
  常见原因是服务没启动、掌机和 PC 不在同一网段、或 Windows 防火墙拦了。
  放行命令（管理员 PowerShell）：

  ```
  New-NetFirewallRule -DisplayName "PC Monitor 8765" -Direction Inbound `
    -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
  ```

- **画面上下颠倒**：设置页里关掉"预旋转 180°"。
  Miyoo 面板是倒装的，Onion 自带播放器同样用 `-vf hflip,vflip` 处理；这里改成在
  PC 端预旋转，省掉掌机的滤镜开销。

- **画面卡顿 / 延迟增长**：设置页把刷新速率降到 5 或 2，或把画质降到 60。

- **GPU 显示"未检测到"**：`nvidia-smi` 不在 PATH 或不是 NVIDIA 卡。本项目的 GPU
  数据只走 `nvidia-smi`（GPU 前三不受影响，它走性能计数器）。

- **端口被占用**：服务启动时会直接报错退出，而不是悄悄和另一份共用端口。
  Windows 上 `SO_REUSEADDR` 允许第二个进程绑同一个端口，请求会随机落到其中一个
  socket 上——所以这里显式关掉了地址复用。看到这个报错就是已经有一份在跑了。

## 已知限制

- **CPU 温度和功耗依赖 MSI Afterburner 在运行**。建议把 Afterburner 设成开机启动，
  它和 RTSS 是一套。GPU 温度不依赖它（来自 `nvidia-smi`）。
- **当日流量只统计服务运行期间**，见上面「当日流量」。
- **每核心频率在 Afterburner 没运行时不显示**（只画占用条）。
- GPU 前三的百分比是各引擎之和，可能和任务管理器某一列不完全一致。
- 掌机端在等 PC 时是**黑屏**，不显示提示——Onion 的 `infoPanel` 会抢占
  framebuffer 且行为不稳，故未采用。日志里能看到状态。
- 发现只扫**本机所在的 /24 网段**，跨网段的 PC 需要手动往 `hosts.txt` 里加一行
  `IP|名字`（下一次扫描出结果时会被覆盖）。
- 按键之间隔得太近（不到约 2 秒）时，后一次按键会覆盖前一次还没被处理的那次——
  切流本身要 1–2 秒，这里就按"最后一次按键生效"处理，没有做按键队列。
