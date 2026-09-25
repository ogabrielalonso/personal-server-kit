import re
import unittest

from pskit import render
from pskit.render import RenderError, exec_line, render_text

PH = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


class RenderTest(unittest.TestCase):
    def test_missing_placeholder(self):
        with self.assertRaises(RenderError):
            render_text("x={{a}} y={{b}}", {"a": 1})

    def test_newline_only_in_blocks(self):
        with self.assertRaises(RenderError):
            render_text("{{a}}", {"a": "x\ny"})
        self.assertEqual(render_text("{{a_block}}", {"a_block": "x\ny"}), "x\ny")

    def test_exec_quoting(self):
        self.assertEqual(exec_line(["/usr/bin/python3", "-m", "brain"]), "/usr/bin/python3 -m brain")
        self.assertEqual(exec_line(["/bin/echo", "a b", "50%", "$HOME"]),
                         '/bin/echo "a b" 50%% "$$HOME"')

    def test_every_template_renders_with_its_placeholders(self):
        for folder in ("systemd", "misc"):
            for name in render.template_names(folder):
                text = (render.TEMPLATES / folder / name).read_text()
                values = {k: "x" for k in PH.findall(text)}
                out = render.render(f"{folder}/{name}", values)
                self.assertNotIn("{{", out, name)


if __name__ == "__main__":
    unittest.main()
