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


class TestParseNvidiaSmiOutput(unittest.TestCase):
    def test_single_gpu(self):
        self.assertEqual(install.parse_nvidia_smi_output("8188\n"), 8188)

    def test_multi_gpu_takes_max(self):
        self.assertEqual(install.parse_nvidia_smi_output("8188\n24576\n"), 24576)

    def test_empty_output(self):
        self.assertIsNone(install.parse_nvidia_smi_output(""))

    def test_garbage_output(self):
        self.assertIsNone(install.parse_nvidia_smi_output("not a number\n"))

    def test_mixed_garbage_and_numbers_ignores_garbage(self):
        self.assertEqual(install.parse_nvidia_smi_output("not a number\n16384\n"), 16384)


class TestResolveVllmTier(unittest.TestCase):
    def test_none_vram_is_none(self):
        self.assertIsNone(install.resolve_vllm_tier(None))

    def test_below_lowest_threshold_is_none(self):
        self.assertIsNone(install.resolve_vllm_tier(8191))

    def test_8188_mib_dev_laptop_is_none(self):
        # The exact boundary case this feature was designed around: an
        # "8GB" laptop GPU reports 8188 MiB, strictly under 8192.
        self.assertIsNone(install.resolve_vllm_tier(8188))

    def test_lower_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(8192)
        self.assertEqual(model, "Qwen/Qwen2.5-3B-Instruct-AWQ")
        self.assertEqual(max_len, 4096)
        self.assertEqual(util, "0.85")

    def test_upper_edge_of_lower_tier(self):
        model, _, _ = install.resolve_vllm_tier(16383)
        self.assertEqual(model, "Qwen/Qwen2.5-3B-Instruct-AWQ")

    def test_middle_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(16384)
        self.assertEqual(model, "Qwen/Qwen2.5-7B-Instruct-AWQ")
        self.assertEqual(max_len, 2048)
        self.assertEqual(util, "0.8")

    def test_upper_edge_of_middle_tier(self):
        model, _, _ = install.resolve_vllm_tier(24575)
        self.assertEqual(model, "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_top_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(24576)
        self.assertEqual(model, "Qwen/Qwen2.5-14B-Instruct-AWQ")
        self.assertEqual(max_len, 4096)
        self.assertEqual(util, "0.85")

    def test_way_above_top_tier(self):
        model, _, _ = install.resolve_vllm_tier(81920)
        self.assertEqual(model, "Qwen/Qwen2.5-14B-Instruct-AWQ")


class TestResolveVllmConfig(unittest.TestCase):
    def test_mode_mock_forces_mock_regardless_of_vram(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "mock"}, 81920)
        self.assertEqual(cfg, {"mock": True, "model": None, "max_model_len": None, "gpu_mem_util": None})

    def test_auto_with_no_gpu_is_mock(self):
        cfg = install.resolve_vllm_config({}, None)
        self.assertTrue(cfg["mock"])

    def test_auto_below_threshold_is_mock(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "auto"}, 8188)
        self.assertTrue(cfg["mock"])

    def test_auto_picks_small_tier(self):
        cfg = install.resolve_vllm_config({}, 10000)
        self.assertEqual(cfg["mock"], False)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-3B-Instruct-AWQ")

    def test_auto_picks_default_tier(self):
        cfg = install.resolve_vllm_config({}, 20000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_auto_picks_large_tier(self):
        cfg = install.resolve_vllm_config({}, 30000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-14B-Instruct-AWQ")

    def test_auto_with_override_keeps_tier_sizing_but_swaps_model(self):
        cfg = install.resolve_vllm_config({"VLLM_MODEL_OVERRIDE": "custom/model"}, 10000)
        self.assertEqual(cfg["model"], "custom/model")
        self.assertEqual(cfg["max_model_len"], 4096)  # small tier's sizing, per 10000 MiB
        self.assertEqual(cfg["gpu_mem_util"], "0.85")

    def test_real_mode_with_no_vram_falls_back_to_default_tier(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real"}, None)
        self.assertEqual(cfg["mock"], False)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_real_mode_with_override_and_no_vram(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real", "VLLM_MODEL_OVERRIDE": "custom/model"}, None)
        self.assertEqual(cfg["model"], "custom/model")
        self.assertEqual(cfg["max_model_len"], 2048)  # fallback default tier's sizing

    def test_real_mode_respects_detected_vram_tier(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real"}, 30000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-14B-Instruct-AWQ")


if __name__ == "__main__":
    unittest.main()
