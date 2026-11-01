"""Inspect the supplied LRO parameter script as data; do not import or execute it."""

import ast
import hashlib
from pathlib import Path

MAX_SCRIPT_BYTES = 512 * 1024
REQUIRED = {"MODEL_PARAMS", "PN_SETTINGS", "MODEL_PN_SETTINGS", "CONFIG_CODE", "QUERY_CODE",
            "PNE71_PARAMETER_NUMBER", "PNE71_OVERVOLTAGE_LEVEL"}


def inspect_lro_script(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError("参数脚本不是文本或超过512KiB")
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise ValueError("参数脚本语法无效") from error
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        else:
            continue
        if name in REQUIRED:
            if name in constants:
                raise ValueError("参数清单重复定义：" + name)
            try:
                constants[name] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise ValueError("参数不是静态字面量，不执行脚本计算：" + name) from error
    if set(constants) != REQUIRED:
        raise ValueError("不能识别完整的LRO参数表")
    timeout = None
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "expected_values"]
    if len(functions) == 1:
        for node in functions[0].body:
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name) and node.test.left.id == "key"
                    and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "can_timeout"
                    and len(node.body) == 1 and isinstance(node.body[0], ast.Return)):
                try:
                    minimum, maximum = ast.literal_eval(node.body[0].value)
                except (ValueError, TypeError):
                    continue
                if type(minimum) in (int, float) and type(maximum) in (int, float):
                    timeout = maximum
    warnings = [
        "此清单来自脚本静态检查；未读取电机，没有执行任何CAN请求或写入。",
        "MIT编码范围不是低速检查的允许力矩/电流；不得直接拿最大值作为探测阈值。",
        "Pn字段的设备含义、固件兼容性及掉电持久性须核对设备资料。",
        "现有脚本按配置中的LRO电机执行；--interface仅筛选总线，不能代表单电机选择。",
        "read模式也会主动发送CAN查询；需与运控安排互斥维护时段。",
    ]
    if timeout == 0:
        warnings.append("脚本将CAN_TIMEOUT设为0（禁用）；不得据此承诺通信中断自动停止。")
    elif timeout is None:
        warnings.append("无法静态确认CAN_TIMEOUT；不能推定已经启用失联保护。")
    return {
        "schema_version": 1, "script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "operation": "motor_parameter_write_and_readback", "firmware_binary_upgrade": False,
        "model_parameters": constants["MODEL_PARAMS"], "common_pn": constants["PN_SETTINGS"],
        "model_pn": constants["MODEL_PN_SETTINGS"],
        "pne71_number": constants["PNE71_PARAMETER_NUMBER"],
        "pne71_value": constants["PNE71_OVERVOLTAGE_LEVEL"],
        "can_timeout_expected_ms": timeout,
        "robot_execution_enabled": False, "warnings": warnings,
    }


def inspect_lro_script_file(path):
    path = Path(path)
    if path.stat().st_size > MAX_SCRIPT_BYTES:
        raise ValueError("参数脚本超过512KiB")
    return inspect_lro_script(path.read_text(encoding="utf-8"))
