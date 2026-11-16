import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant", "src"))

os.environ["MUMU_HOME"] = r"D:\Program Files\Netease\MuMu"
os.environ["MUMU_VM_INDEX"] = "0"

import gui
gui.main()
