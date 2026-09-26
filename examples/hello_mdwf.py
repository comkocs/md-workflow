"""打印工单台「位表」里的位名(30 分钟 demo 里 0 号做的 10 行小事)。"""
import json, pathlib

def slots():
    cfg = pathlib.Path(__file__).with_name("desk_config.json")
    return [row["名字"] for row in json.loads(cfg.read_text(encoding="utf-8"))["位表"]]

def test_slots_has_backend():
    assert "后端" in slots()

if __name__ == "__main__":
    print(" / ".join(slots()))
