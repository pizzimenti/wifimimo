"""Cross-file facts that no single module can check on its own."""

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import wifimimo_shared as shared

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "polkit" / "io.github.pizzimenti.wifimimo.policy"
DISPATCHER = ROOT / "nm" / "90-wifimimo"
QML_DIR = ROOT / "plasmoid" / "org.kde.plasma.wifimimo" / "contents" / "ui"


def _policy_action():
    tree = ET.parse(POLICY)
    actions = tree.getroot().findall("action")
    assert len(actions) == 1
    return actions[0]


def test_policy_action_id_and_exec_path_match_shared():
    action = _policy_action()
    assert action.get("id") == shared.POLKIT_ACTION
    annotate = {a.get("key"): a.text for a in action.findall("annotate")}
    assert annotate["org.freedesktop.policykit.exec.path"] == str(shared.HELPER_PATH)


def test_policy_only_active_sessions_skip_auth():
    defaults = _policy_action().find("defaults")
    assert defaults.find("allow_active").text == "yes"
    assert defaults.find("allow_any").text == "auth_admin"
    assert defaults.find("allow_inactive").text == "auth_admin"


def test_dispatcher_is_valid_sh_and_calls_the_helper():
    subprocess.run(["sh", "-n", str(DISPATCHER)], check=True)
    text = DISPATCHER.read_text()
    assert f"{shared.HELPER_PATH} multipath apply" in text
    assert str(shared.MULTIPATH_FLAG) in text


def test_helper_shebang_isolates_python():
    first = (ROOT / "wifimimo-helper").read_text().splitlines()[0]
    assert first == "#!/usr/bin/python3 -I"


def test_qml_helper_path_matches_shared():
    literals = set()
    for qml in QML_DIR.glob("*.qml"):
        literals |= set(re.findall(r'helperPath:\s*"([^"]+)"', qml.read_text()))
    assert literals == {str(shared.HELPER_PATH)}


def test_qml_only_runs_allowlisted_helper_verbs():
    text = (QML_DIR / "main.qml").read_text()
    # every helper command is built from the allow-list, never free text
    assert '"multipath": ["enable", "disable"]' in text
    assert '"internal": ["enable", "disable"]' in text
    assert 'allowed.indexOf(action) < 0' in text


def test_install_targets_match_shared():
    text = (ROOT / "install.sh").read_text()
    assert f'TARGET_LIB_DIR="{shared.LIB_DIR}"' in text
    assert 'TARGET_HELPER="$TARGET_LIB_DIR/wifimimo-helper"' in text
    assert f'ETC_DIR="{shared.ETC_DIR}"' in text
    assert f'INTERNAL_RULE="{shared.INTERNAL_UDEV_RULE}"' in text
    assert 'TARGET_POLICY="/usr/share/polkit-1/actions/io.github.pizzimenti.wifimimo.policy"' in text
    assert 'TARGET_DISPATCHER="$TARGET_DISPATCHER_DIR/90-wifimimo"' in text


def test_install_ships_every_module_the_daemon_imports():
    text = (ROOT / "install.sh").read_text()
    for module in ("phy_modes.py", "wifimimo_core.py", "wifimimo_shared.py",
                   "wifimimo_radio.py", "wifimimo_nm.py"):
        assert module in text, module


def test_rt_protos_entry_matches_shared():
    text = (ROOT / "install.sh").read_text()
    assert f"printf '{shared.RT_PROTO}\\t{shared.RT_PROTO_NAME}\\n'" in text


def test_versions_agree():
    version = (ROOT / "VERSION").read_text().strip()
    meta = json.loads((ROOT / "plasmoid" / "org.kde.plasma.wifimimo" / "metadata.json").read_text())
    assert meta["KPlugin"]["Version"] == version
    readme = (ROOT / "README.md").read_text()
    assert f"Current version: `{version}`" in readme
    changelog = (ROOT / "CHANGELOG.md").read_text()
    releases = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert releases and releases[0] == version
