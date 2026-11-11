# 单次活动可获取数据清单（佳明国区）

这份文档记录 `connect.garmin.cn/gc-api` 的接口分层和字段结构，供解析与扩展时参考。
**所有示例数值均已移除**，只保留字段口径；真实活动数据一律不入库到版本控制。

## 数据来源分层

| 层级 | 接口 | 内容 | 项目现状 |
|---|---|---|---|
| L1 汇总 | `activities(limit=N)` 列表里每个对象 | 约 109 个字段，含心率五区时间、训练效果、跑步动态 | 已落库（raw 列） |
| L2 分圈 | `/activity-service/activity/{id}/splits` | 每圈约 41 字段 | 按需读取 |
| L2 分段 | `/activity-service/activity/{id}/typedsplits` | 按距离类型切分 | 未落库 |
| L3 时序 | `/activity-service/activity/{id}/details` | 23 个通道，1Hz 全量采样 + GPS 轨迹 | 按需读取并缓存 |
| L4 天气 | `/activity-service/activity/{id}/weather` | 气温、体感、湿度、风向风速、天况 | 未落库 |
| L5 基线 | `/userprofile-service/userprofile/user-settings` | 体重、身高、生日、VO2max、乳酸阈心率/配速 | 不落库（每次现取） |
| L5 当日 | `/metrics-service/metrics/maxmet/daily/{d}/{d}` | 当日 VO2max 精确值、fitness age | 未落库 |

## L1 汇总字段分组

| 组 | 字段 |
|---|---|
| 元信息 | `activityId` / `activityName` / `activityType.typeKey` / `activityUUID` / `startTimeLocal` / `startTimeGMT` / `beginTimestamp` / `timeZoneId` / `locationName` / `ownerFullName` / `deviceId` / `manufacturer` / `lapCount` / `hasPolyline` / `hasSplits` / `isManualActivity` / `privacy` / `isPR` |
| 时间距离 | `distance` / `duration` / `elapsedDuration` / `movingDuration` / `endTimeGMT` / `minActivityLapDuration` |
| 配速速度 | `averageSpeed` / `maxSpeed` / `avgGradeAdjustedSpeed` / `fastestSplit_1000` / `fastestSplit_1609` / `fastestSplit_5000` / `fastestSplit_10000` |
| 心率 | `averageHR` / `maxHR` / `hrTimeInZone_1..5`（秒） |
| 跑步动态 | `averageRunningCadenceInStepsPerMinute` / `maxRunningCadenceInStepsPerMinute` / `maxDoubleCadence` / `steps` / `avgStrideLength` / `avgGroundContactTime` / `avgVerticalOscillation` / `avgVerticalRatio` / `maxVerticalSpeed` |
| 功率 | `avgPower` / `maxPower` / `normPower` / `powerTimeInZone_1..5` / `sumAccumulatedPower`（在时序里） |
| 负荷能耗 | `activityTrainingLoad` / `aerobicTrainingEffect` / `anaerobicTrainingEffect` / `trainingEffectLabel` / `aerobicTrainingEffectMessage` / `anaerobicTrainingEffectMessage` / `calories` / `bmrCalories` / `waterEstimated` / `differenceBodyBattery` / `moderateIntensityMinutes` / `vigorousIntensityMinutes` |
| 海拔 | `elevationGain` / `elevationLoss` / `minElevation` / `maxElevation` / `avgElevation` / `isElevationCorrected` |
| 位置轨迹 | `startLatitude` / `startLongitude` / `endLatitude` / `endLongitude` / `hasHeatMap` / `hasImages` / `hasVideo` |
| 体能生理 | `vO2MaxValue`（活动内嵌）/ `vo2MaxPreciseValue`（当日 maxmet 接口） |
| 其余 | `eventType` / `isFavorite` / `isAutoCalcCalories` / `isParent` / `parent` / `purposeful` / `userPro` / `sportTypeId` / `summarizedDiveInfo` / `isDecoDive` / `decoDive` / `qualifyingDive` / `isAtpActivity` / `atpActivity` / `isPurposeful` / `manualActivity` / `elevationCorrected` / `favorite` / `pr` |

> `userRoles`（权限字符串）、`ownerProfileImageUrl*`（头像）以及
> `ownerFullName` / `deviceId` / 起终点经纬度属于个人标识与精确位置，
> **不写入本地数据库、不出现在任何产物里**。

单圈 41 字段还包括：`movingDuration` / `elapsedDuration` / `elevationGain|Loss` /
`max|minElevation` / `averageMovingSpeed` / `maxSpeed` / `calories` / `bmrCalories` /
`averagePower` / `maxPower` / `minPower` / `normalizedPower` / `totalWork` /
`directWorkoutComplianceScore` / `avgGradeAdjustedSpeed` / `lapIndex` /
`wktStepIndex` / `intensityType` / `lengthDTOs`（泳池用）/ `messageIndex` / 起止经纬度。

## L3 时序通道（23 个，1Hz）

```
directTimestamp / sumDuration / sumElapsedDuration / sumMovingDuration / sumDistance /
directSpeed / directGradeAdjustedSpeed / directHeartRate / directRunCadence / directDoubleCadence /
directFractionalCadence / directStrideLength / directGroundContactTime / directVerticalOscillation /
directVerticalRatio / directVerticalSpeed / directPower / sumAccumulatedPower /
directElevation / directLatitude / directLongitude / directBodyBattery / directPerformanceCondition
```

- 全量 `totalMetricsCount` 约等于活动秒数，可用 `?maxChartSize=` 降采样
- GPS 轨迹：`lat` / `lon` / `speed` / `altitude`（常为 null）/ `cumulativeAscent`

## L4 天气

`temp` / `apparentTemp` / `dewPoint` / `relativeHumidity` / `windDirection` /
`windSpeed` / `windGust` / `weatherTypeDTO.desc` / `weatherStationDTO`。
华氏与摄氏并存，需按项目内约定统一。

## L5 用户基线

`gender` / `weight` / `height` / `birthDate` / `vo2MaxRunning` /
`lactateThresholdHeartRate` / `lactateThresholdSpeed`。

> 乳酸阈配速的原始单位不直观，项目内按实测校准后使用；见 `garmin_cn/metrics.py`。

## 已知拿不到 / 需额外条件

- 左右触地平衡、左右功率平衡：需要 HRM-Pro / Rally 等双模设备
- 恢复时间（Recovery Time）、训练状态标签：属于日级数据，不在活动对象内
- 心率区间边界数值：`/biometric-service/biometric/hrZones` 可能返回 null，只能用服务端算好的 `hrTimeInZone_*`
- 心率区间名称：活动 raw 与详情 payload 实测只有时长，没有 `zoneName` / `zoneFloor`。一图流的中文副名由 `garmin_cn/hrzones.py` 按佳明 Connect 官方默认名（热身 / 轻松 / 有氧 / 阈值 / 极限）提供；在 Connect 里改过名称时，用 `config.json` 的 `hr_zone_names`（5 个字符串，Z1→Z5）覆盖
- 能量来源（碳水/脂肪占比）、核心体温、血氧曲线：本设备/本接口未提供
