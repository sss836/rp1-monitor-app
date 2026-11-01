"""Version-bound deploy gains for preparation and supporting physical motors.

The arrays use the deploy YAML's physical motor order, before logical-joint
mapping. They are the unchanged deploy gains, without adaptive gain increases
or gravity compensation. A matching profile is not hardware safety validation.
"""

_PROFILES = {
    24: {
        "robot_yaml_path": "src/inference/robots/rp1_24dof/robot.yaml",
        "robot_yaml_sha256": "9f1ff9c6b7399b6b82e280304a008b50222cbbdfdba1bf806c07742b13a8428a",
        "kp": (
            120.0, 120.0, 90.0, 120.0, 40.0, 40.0, 150.0,
            120.0, 120.0, 90.0, 120.0, 40.0, 40.0, 150.0,
            30.0, 30.0, 20.0, 30.0, 20.0,
            30.0, 30.0, 20.0, 30.0, 20.0,
        ),
        "kd": (
            9.0, 9.0, 6.0, 9.0, 2.0, 2.0, 12.5,
            9.0, 9.0, 6.0, 9.0, 2.0, 2.0, 12.5,
            2.5, 2.5, 1.5, 2.5, 1.5,
            2.5, 2.5, 1.5, 2.5, 1.5,
        ),
    },
    28: {
        "robot_yaml_path": "src/inference/robots/rp1/robot.yaml",
        "robot_yaml_sha256": "0cdc96af415557a62ea93508af31d31b7fb3a359b053493f0b3c2efb446e9588",
        "kp": (
            120.0, 120.0, 90.0, 150.0, 40.0, 40.0, 250.0,
            120.0, 120.0, 90.0, 150.0, 40.0, 40.0, 300.0,
            40.0, 40.0, 20.0, 30.0, 20.0, 20.0, 20.0,
            40.0, 40.0, 20.0, 30.0, 20.0, 20.0, 20.0,
        ),
        "kd": (
            8.0, 8.0, 6.0, 10.0, 2.0, 2.0, 20.0,
            8.0, 8.0, 6.0, 10.0, 2.0, 2.0, 25.0,
            3.0, 3.0, 1.0, 1.5, 1.0, 1.0, 1.0,
            3.0, 3.0, 1.0, 1.5, 1.0, 1.0, 1.0,
        ),
    },
}


def preparation_gain_profile(dof):
    """Return a fresh profile; callers still verify the observed deploy hash."""
    if type(dof) is not int or dof not in _PROFILES:
        raise ValueError("复位增益配置仅支持24或28电机")
    profile = _PROFILES[dof]
    return dict(
        profile_id=f"rp1-{dof}-deploy-preparation-v1",
        dof=dof,
        motor_order="physical_motor",
        robot_yaml_path=profile["robot_yaml_path"],
        robot_yaml_sha256=profile["robot_yaml_sha256"],
        kp=list(profile["kp"]),
        kd=list(profile["kd"]),
        ramp_s=2.0,
    )


def _exact(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (actual.keys() == expected.keys()
                and all(_exact(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return (len(actual) == len(expected)
                and all(_exact(a, b) for a, b in zip(actual, expected)))
    return actual == expected


def validate_preparation_gain_profile(profile, dof):
    """Reject incomplete or modified profiles and return an independent copy.

    This checks the pinned configuration data. The runtime must separately
    verify the robot YAML's actual SHA256 before using its gains on hardware.
    """
    expected = preparation_gain_profile(dof)
    if not _exact(profile, expected):
        raise ValueError("复位增益配置与版本绑定配置不符")
    return expected
