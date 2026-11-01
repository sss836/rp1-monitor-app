# 模组输出力矩估算

当前接入位置：上位机“角度与标定 → 单侧限位检查 → 模拟闭环”。实时表格只显示关节与估算力矩（N·m）；结果文件保留原始电流、型号及换算来源，不在主页面展示换算过程。本轮没有连接机器人或修改机器人文件。

## 型号与换算

| motor_model | 模组名称 | deploy枚举 | 输出端系数（N·m/A） |
| --- | --- | --- | ---: |
| 0 | PJ2 55 | LRO_5550 | 1.74 |
| 1 | PJ3 60 | LRO_6562 | 1.62 |
| 2 | PJ3 75 | LRO_8462 | 2.31 |
| 3 | PJ3 97 | LRO_10062 | 2.38 |

型号与当前本机 `roboparty_deploy` 的枚举、参数表对应。系数来自用户提供的 `deploy_motorlro_params (1).py`，来源 SHA256 为 `34f2214d52cf5e879e9ec169ec91725b22cdc99228a47d2671a491c9c85b0f3c`。

本机静态配置中，24电机版包含8台PJ3 97、4台PJ3 75和12台PJ3 60；28电机版另含4台PJ2 55。这只描述本机配置，现场电机仍按CAN接口与ID绑定。

计算为 `tau_est = Iq × coefficient`，正负号保留电机坐标方向。系数按该脚本解释为模组输出端系数，不再次乘减速比；协议Type1已是等幅Park的Iq，也不再乘√2。例如PJ2 55的3 A得到5.22 N·m，PJ3 97的−3 A得到−7.14 N·m。

这是单模组输出轴估算。闭链踝部应先对两台电机的原始Iq分别换算，再结合机构构型映射到踝关节空间；当前不将ROS闭链effort或母线电流自动输入该函数。

## 使用

```bash
cd rp1-monitor-app
./start-monitor.sh
```

在上述面板选择：

- `examples/limit_execution_simulated.json`
- `examples/limit_execution_simulated.urdf`

点击“参数预检”“开始模拟闭环”即可查看。两轴合成夹具分别使用型号0和3演示，并非实际RP1数据。数据源标签保持 `simulated`；页面采用“估算力矩”名称，不增加单独的精度提示栏。

执行配置中的可选字段：

```json
{
  "torque_estimation_model": 0,
  "torque_input_kind": "motor_iq"
}
```

必须同时提供，且枚举只能是0–3。缺少映射、非有限值、超候选Iq编码量程或不同物理量不会生成估算值。没有自动关联电池电流、母线电流、相电流RMS或已有ROS effort。

静态deploy预检会根据每个物理电机的model附加对应系数，并保留其逻辑索引、CAN接口、ID和闭链配对；配置读取不发送CAN命令。

## 数据与结果

`result.json` 的逐帧 `samples[].torque_estimation` 包含原始Iq、估算值及 `profile_key`；完整来源与换算假设仅在 `torque_profiles` 中保存一次，避免每帧重复大段文字。`outcomes[].torque_estimation_at_detection` 保存候选接触时的同一反馈帧估算及序号。

精度审查与协议证据保存在 [核对报告](../reviews/LRO_TORQUE_EVIDENCE_20261030.md)。由于没有原始台架成对数据，结果中的误差/不确定度保持空值，不填造RMSE或百分比；页面不显示这些审查字段。已有原始Iq和历史记录不被覆盖。

这次接入用于显示与留存，不会用候选系数自动替换运动中止阈值、写电机参数或启用硬件控制。当前执行器仍只有模拟驱动，电流过限保护保留。
