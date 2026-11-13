# 高驰活动卡数据口径

2026-11-10 核对 Training Hub 原始活动明细与卡片。真实活动、凭据、轨迹和截图只留在被 Git 忽略的 data/output 目录。

| 卡片内容 | 数据来源和处理 |
|---|---|
| 名称、日期、起跑时间、运动类型 | 活动列表 name/startTime/sportType；沿用同步记录 |
| 距离 | 明细 summary.distance 厘米 / 100 → 米 |
| 用时 | summary.totalTime 厘秒 / 100 → 秒；与 workoutTime 的有效运动用时区分 |
| 平均配速 / 骑行均速 | 按 speedUnit 解释 avgSpeed：1 是 km/h×100，3 是秒/公里；avgSpeedV2 固定为 km/h×100。优先设备原生均值，缺失才以距离 / 有效运动用时（再退到总用时）计算；内部统一为 m/s 或秒/公里 |
| 爬升 | summary.elevGain 米 |
| 全程平均步频、步幅、心率 | summary.avgCadence / avgStepLen / avgHr；禁止用快速跑动子集覆盖 |
| 轨迹 | frequencyList.gpsLat/gpsLon / 1e7；复用公共轨迹清洗、抽稀和隐私裁剪 |
| 心率分区 | zoneList 中 type=126 的 zoneItemList，按 zoneIndex 排序，second 已是秒；保留六区及 leftScope/rightScope 心率范围，界面按区号+分区名称单行显示（恢复、有氧耐力、有氧动力、阈值、无氧耐力、无氧动力），上下限保留在数据层；比例由原生时长计算 |
| 配速 / 速度心率曲线 | frequencyList.speed 根据自身或 summary.speedUnit 归一，heart 为 bpm；缺少单位的旧格式按秒/公里兼容。显示曲线抽稀、平滑，不由累计距离反推高驰配速 |
| 步频步幅曲线 | frequencyList.cadence / cadenceLength；保留慢跑、步行，不翻倍、不钳制、不由速度反算步幅 |
| 触地、垂直振幅 | 优先有效 summary；缺失/占位零时取 graphList 按 key 匹配的 graphItem.avg：groundTime 为 ms，verticalVibration 为 mm / 10 → cm。不取单圈/单点冒充全程均值，不混用 verticalStrideRatio；缺失显示 — |
| 天气 | weather.temperature / bodyFeelTemp / humidity / windSpeed 除以 10，归一为 ℃ / ℃ / % / km/h；windDirectionType 为 N、NE、E、SE、S、SW、W、NW。天气描述按 provider=1（AccuWeather）或 2（心知）分别映射 weatherType；缺失不补当前天气，复用佳明卡片展示 |
| 训练负荷、热量 | summary.trainingLoad / calories（cal / 1000 → kcal） |
| 有氧、无氧 TE | summary.aerobicEffect / anaerobicEffect；缺失显示 —，真实零保留 |
| 主要益处 | 无已验证的高驰标签映射，显示 —，不把缺失误判为热身 |
| 分圈 | 优先 lapDistance=100000 的公里组，排除 type=-1 全程汇总；distance 厘米、time 厘秒 |
| 分圈合并 | 距离时间求和，心率步频按圈时长加权；是本地合并显示，非高驰额外分组 |
| 间歇版式判定 | 高驰**没有**佳明 `hasIntensityIntervals` 那样的官方标记（官方文档与真机明细均未见，诊断日志 `coros.training` 持续确认）。改看结构证据：lapList 存在**手动计圈组**（整组圈距明显不是 1 km）且通过严格门槛的快慢交替（快慢各 ≥3 组、切换 ≥5 次、速度差 ≥1.35×）才切双栏；整齐公里圈（越野/等红灯）一律默认版式。明细带 `lap_groups`（各组圈距/圈数）供判定 |

2026-11-11 跑步动态、心率分区和天气适配版本升级为 12，旧明细缓存通过 ensure 自动失效并重抓。图表仍做抽稀和平滑，曲线不是逐点原始数据导出。当前未映射的字段不代表高驰设备一定没有记录。

验证：tools/test_coros_source.py 的离线回归覆盖单位、原生汇总、慢速步态、缺失值、六区时长、分圈选择、加权合并和缓存版本；另读取截图对应的真实活动明细重新出图核验。

跑步动态核验：本地原始明细 summary 两项为 0，但 graphList 给出 groundTime.avg=248、verticalVibration.avg=70，归一后为 248 ms、7.0 cm；首圈 groundTime=247 不作为全程均值。离线回归覆盖图表顺序、单位、无效值、汇总优先级、卡片显示与旧缓存失效。

2026-11-13 骑行均速修复：此前将所有 `avgSpeed` 当成跑步的秒/公里，导致 `speedUnit=1, avgSpeed=2500`（25 km/h）被误算为 1.4 km/h。现统一按单位解析平均速度、最高速度与逐点速度，保留原生平均速度与总用时的区别；列表不再先把骑行配速取整。历史列表会从保存的原始字段离线重新解释均速，无需重新导入全部活动；明细缓存升级至 14，打开时重新抓取以更新速度曲线和最高速度。

单位核对于高驰官方前端的 `getSpeedValueWithSpeedType` / `getSpeedUnitText`：`https://static.coros.com/coros-traininghub-v2/assets/main-5sqL4v9u.js`，以及活动详情的图表与汇总实现 `https://static.coros.com/coros-traininghub-v2/assets/index-OAlwoEhY.js`。回归覆盖 25 km/h 的平均速度、45 km/h 的最高速度、暂停与有效用时差异、短距离骑行、旧数据库字段兼容、跑步及佳明原有口径。

天气换算与枚举核对于 2026-11-11 高驰官方公开前端：`https://static.coros.com/coros-traininghub-v2/assets/index-OAlwoEhY.js` 和 `https://static.coros.com/locale/coros-traininghub-v2/zh-CN.prod.js`。本地明细样本归一为 19℃ / 湿度70% / 晴 / 东南风7.3 km/h；另一份为14℃ / 湿度99% / 雾 / 东北风5 km/h。
