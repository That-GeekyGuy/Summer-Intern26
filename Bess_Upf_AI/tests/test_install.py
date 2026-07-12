import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import install


class TestRenderKindConfig(unittest.TestCase):
    def test_maps_local_port_to_pinned_node_port(self):
        text = install.render_kind_config(9090)
        self.assertIn("containerPort: {}".format(install.INGRESS_NODE_PORT), text)
        self.assertIn("hostPort: 9090", text)

    def test_preserves_three_worker_mounts(self):
        text = install.render_kind_config(8080)
        self.assertEqual(text.count("role: worker"), 3)
        self.assertEqual(text.count("containerPath: /models"), 3)

    def test_default_port_example(self):
        text = install.render_kind_config(8080)
        self.assertIn("hostPort: 8080", text)


if __name__ == "__main__":
    unittest.main()
