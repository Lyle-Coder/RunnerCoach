# 高驰活动卡数据口径

2026-11-10 核对 Training Hub 原始活动明细与卡片。真实活动、凭据、轨迹和截图只留在被 Git 忽略的 data/output 目录。

| 卡片内容 | 数据来源和处理 |
|---|---|
| 名称、日期、起跑时间、运动类型 | 活动列表 name/startTime/sportType；沿用同步记录 |
| 距离 | 明细 summary.distance 厘米 / 100 → 米 |
| 用时 | summary.totalTime 厘秒 / 100 → 秒；与 workoutTime 的有效运动用时区分 |
| 平均配速 | summary.avgSpeed 秒/公里；缺失才用列表/距离时间兜底 |
| 爬升 | summary.elevGain 米 |
| 全程平均步频、步幅、心率 | summary.avgCadence / avgStepLen / avgHr；禁止用快速跑动子集覆盖 |
| 轨迹 | frequencyList.gpsLat/gpsLon / 1e7；复用公共轨迹清洗、抽稀和隐私裁剪 |
| 心率分区 | zoneList 中 type=126 的 zoneItemList，按 zoneIndex 排序，second 已是秒；保留六区，比例由原生时长计算 |
| 配速心率曲线 | frequencyList.speed 秒/公里和 heart；显示曲线抽稀、平滑，不由累计距离反推高驰配速 |
| 步频步幅曲线 | frequencyList.cadence / cadenceLength；保留慢跑、步行，不翻倍、不钳制、不由速度反算步幅 |
| 触地、垂直振幅 | 当前未接入经验证的高驰字段映射，显示缺失，不借用佳明字段值 |
| 训练负荷、热量 | summary.trainingLoad / calories（cal / 1000 → kcal） |
| 有氧、无氧 TE | summary.aerobicEffect / anaerobicEffect；缺失显示 —，真实零保留 |
| 主要益处 | 无已验证的高驰标签映射，显示 —，不把缺失误判为热身 |
| 分圈 | 优先 lapDistance=100000 的公里组，排除 type=-1 全程汇总；distance 厘米、time 厘秒 |
| 分圈合并 | 距离时间求和，心率步频按圈时长加权；是本地合并显示，非高驰额外分组 |
| 间歇、前后半程推断 | 高驰卡不使用佳明路径的速度聚类和快速样本推断，避免越野坡度变化被当作间歇 |

适配版本升级为 3，旧明细缓存通过 ensure 自动失效并重抓。图表仍做抽稀和平滑，曲线不是逐点原始数据导出。当前未映射的字段不代表高驰设备一定没有记录。

验证：tools/test_coros_source.py 的离线回归覆盖单位、原生汇总、慢速步态、缺失值、六区时长、分圈选择、加权合并和缓存版本；另读取截图对应的真实活动明细重新出图核验。
