# SYNTHETIC DEMO DATA — FICTIONAL — NOT SEAGATE INTERNAL DATA

# SelfTest Failure Code Guide / SelfTest 失败代码说明

## F127 — Positioning Timeout / 定位超时
表示 SelfTest 阶段未在规定演示时间内完成定位。F127 是失败类型，不是根因。设备、物料、测试程序或其他因素均可能产生相似表现。

## F131 — Seek Settle Variance / 寻道稳定偏差
表示寻道稳定行为偏离参考区间。不得与 F127 自动视为同一问题。

## F204 — Read Channel Calibration Fail / 读通道校准失败
表示读通道校准未满足演示检查条件。

## F219 — Head Signal Margin Low / 磁头信号裕量偏低
表示信号裕量低于演示阈值，需要结合产品、物料与站点上下文判断。

## General rule / 通用规则
Failure Code 只能描述观察到的失败类型，不能单独证明最终根因。
