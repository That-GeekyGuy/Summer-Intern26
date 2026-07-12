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


class TestParsePortBinding(unittest.TestCase):
    def test_extracts_host_port_for_matching_container_port(self):
        raw = '{"30080/tcp":[{"HostIp":"","HostPort":"8080"}],"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":"42617"}]}'
        self.assertEqual(install.parse_port_binding(raw, 30080), 8080)

    def test_returns_none_when_container_port_absent(self):
        raw = '{"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":"42617"}]}'
        self.assertIsNone(install.parse_port_binding(raw, 30080))

    def test_returns_none_for_null_bindings(self):
        self.assertIsNone(install.parse_port_binding("null", 30080))

    def test_returns_none_for_empty_object(self):
        self.assertIsNone(install.parse_port_binding("{}", 30080))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(install.parse_port_binding("not json", 30080))

    def test_returns_none_for_empty_entries_list(self):
        raw = '{"30080/tcp":[]}'
        self.assertIsNone(install.parse_port_binding(raw, 30080))


class TestResolveLocalPort(unittest.TestCase):
    def test_uses_env_value(self):
        self.assertEqual(install.resolve_local_port({"LOCAL_PORT": "9090"}), 9090)

    def test_defaults_to_8080_when_unset(self):
        self.assertEqual(install.resolve_local_port({}), 8080)

    def test_exits_on_non_integer(self):
        with self.assertRaises(SystemExit):
            install.resolve_local_port({"LOCAL_PORT": "not-a-port"})


class TestShouldSkipBuild(unittest.TestCase):
    def test_skip_requested_and_cluster_reused_skips(self):
        self.assertTrue(install.should_skip_build(True, False))

    def test_skip_requested_but_cluster_recreated_forces_build(self):
        self.assertFalse(install.should_skip_build(True, True))

    def test_no_skip_requested_and_cluster_reused_builds(self):
        self.assertFalse(install.should_skip_build(False, False))

    def test_no_skip_requested_and_cluster_recreated_builds(self):
        self.assertFalse(install.should_skip_build(False, True))


if __name__ == "__main__":
    unittest.main()
