"""Fresh deployment planning preserves percentages, not obsolete device bindings."""
from pathlib import Path
import runpy
import unittest


PLAN = runpy.run_path(str(Path(__file__).resolve().parents[1] / "control/plan_apk_redeployment.py"))["plan"]


class ApkRedeploymentPlan(unittest.TestCase):
    def inventory(self):
        return [{"id": 1, "devices": [{"id": i, "instance_id": f"00000000-0000-0000-0000-{i:012x}",
                                       "resource_tag": f"{i:012x}"} for i in range(1, 4)]}]

    def test_rounding_and_only_current_inventory(self):
        result = PLAN(self.inventory(), [{"id": 4, "sha256": "a" * 64, "percent": 50,
                                          "targets": [{"host_id": 99, "device_ids": [9]}]}])
        self.assertFalse(result["mutates"])
        deployment = result["deployments"][0]
        self.assertEqual(deployment["selected_count"], 2)
        self.assertEqual(deployment["targets"][0]["host_id"], 1)
        self.assertEqual(deployment["request"]["parameters"], {"apk_id": 4, "percent": 50})

    def test_explicit_targets_need_review_and_overlaps_are_not_hidden(self):
        result = PLAN(self.inventory(), [{"id": 3, "sha256": "c" * 64, "percent": 100, "job_id": 2},
                                         {"id": 2, "sha256": "b" * 64, "percent": 100, "job_id": 1},
                                         {"id": 1, "selection_mode": "devices", "percent": 100}])
        self.assertEqual([row["apk_id"] for row in result["deployments"]], [2, 3])
        self.assertEqual(len(result["overlaps"]), 3)
        self.assertEqual(result["overlaps"][0]["final_apk_id"], 3)
        self.assertEqual(result["skipped"][0]["apk_id"], 1)

    def test_missing_or_duplicate_fresh_identity_is_rejected(self):
        hosts = self.inventory()
        hosts[0]["devices"][0].pop("instance_id")
        with self.assertRaises(ValueError):
            PLAN(hosts, [])
        hosts = self.inventory()
        hosts[0]["devices"].append(hosts[0]["devices"][0])
        with self.assertRaises(ValueError):
            PLAN(hosts, [])


if __name__ == "__main__":
    unittest.main()
