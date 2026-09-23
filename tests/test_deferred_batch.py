import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pdd_ab_image_publish as ab
import pdd_repaired_supplement as supplement
import deferred_publish_batch as batch


class DeferredBatchTests(unittest.TestCase):
    def exercise(self, module, *, failures=None, limit=0, audit_only=False, initial=None, run_again=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paired = module is ab
        folder_name = "pairs" if paired else "products"
        store = {"profile": "test", "venderId": 10, "shopName": "test"}
        rows = [{"sourceSku": name, "originalProductId": 1, "ok": True, "skuCount": index}
                for index, name in enumerate(("one", "two", "three"), 1)]
        module.mover.write_json(root / "prepared.json", {"pairs" if paired else "results": rows})
        module.mover.write_json(root / "store.json", {})
        for row in rows:
            folder = root / folder_name / row["sourceSku"]
            plan = {**row, "store": store, "itemNumber": "TEST-" + row["sourceSku"]}
            module.mover.write_json(folder / "plan.json", plan)
            module.mover.write_json(folder / "dry-run.json", {"confirmToken": ab.digest_json(plan)})
            if initial:
                module.mover.write_json(folder / "state.json", initial)
        events = []
        failures = failures or {}

        def verify(folder, product_id, *unused):
            events.append("verify-" + folder.name)
            return {"ok": True, "productId": product_id}

        def submit(folder, args, *, defer_verification=False):
            events.append("submit-" + folder.name)
            plan = module.mover.read_json(folder / "plan.json")
            module.pipeline.save_state(folder / "state.json", "SUBMITTING", confirmToken=ab.digest_json(plan))
            if folder.name in failures:
                raise failures[folder.name]
            product_id = 100 + len(events)
            module.pipeline.save_state(folder / "state.json", "PUBLISHED", productId=product_id)
            if defer_verification:
                return {"ok": False, "pending": True, "productId": product_id, "stage": "PUBLISHED"}
            return verify(folder, product_id)

        args = SimpleNamespace(output=root, store_config=root / "store.json", profile="test",
                               confirm="" if audit_only else "PUBLISH_TO_JD", limit=limit, skip_failures=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module.pipeline, "verify_store_identity", return_value=store))
            stack.enter_context(patch.object(module.pipeline, "find_existing_product", return_value=None))
            stack.enter_context(patch.object(module.jingmai, "run_jdo_large", side_effect=AssertionError("No network")))
            stack.enter_context(patch.object(module.jingmai, "get", side_effect=AssertionError("No network")))
            stack.enter_context(patch.object(module, "publish_pair" if paired else "publish_one", side_effect=submit))
            stack.enter_context(patch.object(module, "verify_pair" if paired else "verify", side_effect=verify))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            runner = module.run if paired else module.publish
            error = None
            try:
                runner(args, audit_only=True) if audit_only else runner(args)
                if run_again:
                    runner(args, audit_only=True) if audit_only else runner(args)
            except Exception as caught:
                error = caught
        progress_path = root / "progress.json"
        progress = module.mover.read_json(progress_path) if progress_path.is_file() else {}
        return events, progress, error, root

    def test_all_submissions_precede_first_verification(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module)
                self.assertIsNone(error)
                self.assertEqual(events, ["submit-one", "submit-two", "submit-three", "verify-one", "verify-two", "verify-three"])
                self.assertEqual(progress["succeeded"], 3)
                self.assertEqual(progress["pending"], 0)
                self.assertTrue(progress["completed"])

    def test_unknown_submission_is_pending_not_failed_or_retried(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, root = self.exercise(module, failures={"one": TimeoutError("response lost")})
                self.assertIsNone(error)
                self.assertEqual(events, ["submit-one", "submit-two", "submit-three", "verify-two", "verify-three"])
                self.assertEqual(progress["failed"], 0)
                self.assertEqual(progress["awaitingVerification"], 1)
                self.assertEqual(progress["unexecuted"], 0)
                self.assertEqual(progress["stage"], "PARTIAL")
                folder = root / ("pairs" if module is ab else "products") / "one"
                self.assertEqual(module.mover.read_json(folder / "state.json")["stage"], "SUBMITTING")

    def test_limit_counts_unexecuted_items(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, limit=1)
                self.assertIsNone(error)
                self.assertEqual(events, ["submit-one", "verify-one"])
                self.assertEqual(progress["unexecuted"], 2)
                self.assertEqual(progress["pending"], 2)
                self.assertFalse(progress["completed"])

    def test_verify_only_never_calls_submission(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, audit_only=True, initial={"stage": "PUBLISHED", "productId": 222})
                self.assertIsNone(error)
                self.assertEqual(events, ["verify-one", "verify-two", "verify-three"])
                self.assertEqual(progress["succeeded"], 3)

    def test_permission_failure_stops_even_if_store_read_still_works(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, failures={"one": module.jingmai.JdoError("403 forbidden")})
                self.assertIsNotNone(error)
                self.assertEqual(events, ["submit-one"])
                self.assertEqual(progress["stage"], "STOPPED")
                self.assertEqual(progress["unexecuted"], 2)

    def test_repeated_transport_failures_stop_batch(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                _, progress, error, _ = self.exercise(module, failures={name: TimeoutError("response lost") for name in ("one", "two", "three")})
                self.assertIsNotNone(error)
                self.assertEqual(progress["stage"], "STOPPED")
                self.assertEqual(progress["awaitingVerification"], 3)
                self.assertEqual(progress["failed"], 0)

    def test_verify_only_does_not_claim_unsubmitted_items_finished(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, audit_only=True)
                self.assertIsNone(error)
                self.assertEqual(events, [])
                self.assertEqual(progress["unexecuted"], 3)
                self.assertFalse(progress["completed"])

    def test_repeat_batch_only_rechecks_existing_ids(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, run_again=True)
                self.assertIsNone(error)
                self.assertEqual([event for event in events if event.startswith("submit-")], ["submit-one", "submit-two", "submit-three"])
                self.assertEqual(len([event for event in events if event.startswith("verify-")]), 6)
                self.assertEqual(progress["succeeded"], 3)

    def test_repeat_batch_does_not_resubmit_unknown_item(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                events, progress, error, _ = self.exercise(module, failures={"one": TimeoutError("lost")}, run_again=True)
                self.assertIsNone(error)
                self.assertEqual(events.count("submit-one"), 1)
                self.assertEqual(progress["awaitingVerification"], 1)
                self.assertEqual(progress["failed"], 0)

    def test_unlimited_legacy_none_limit_is_supported(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                _, progress, error, _ = self.exercise(module, limit=None)
                self.assertIsNone(error)
                self.assertEqual(progress["succeeded"], 3)


class ReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = {"profile": "test", "venderId": 10, "shopName": "test"}
        self.plan = {"sourceSku": "one", "originalProductId": 1, "store": self.store, "itemNumber": "TEST-one"}
        ab.mover.write_json(self.folder / "plan.json", self.plan)
        ab.mover.write_json(self.folder / "store.json", {})
        ab.pipeline.save_state(self.folder / "state.json", "SUBMITTING", confirmToken=ab.digest_json(self.plan), payloadSha256="retained")
        self.args = SimpleNamespace(store_config=self.folder / "store.json", profile="test")
        self.enterContext(patch.object(batch.pipeline, "verify_store_identity", return_value=self.store))
        self.lookup = self.enterContext(patch.object(batch.pipeline, "find_existing_product", return_value=None))
        self.detail = self.enterContext(patch.object(batch.jingmai, "get", return_value={"data": {"productInfo": {"itemNum": "TEST-one"}}}))
        self.remote_write = self.enterContext(patch.object(batch.jingmai, "run_jdo_large", side_effect=AssertionError("No writes")))
        self.verifier = self.enterContext(patch.object(ab, "verify_pair", return_value={"ok": True, "productId": 222}))

    def test_receipt_recovers_id_and_preserves_apply_id_without_submission(self):
        ab.mover.write_json(self.folder / "publish/response.json", {"data": {"productId": 222, "applyId": 333}})
        result = batch.verify_submitted(self.folder, self.args, self.verifier)
        self.assertTrue(result["ok"])
        self.lookup.assert_not_called()
        self.remote_write.assert_not_called()
        self.verifier.assert_called_once_with(self.folder, 222)
        state = batch.read_state(self.folder)
        self.assertEqual(state["productId"], 222)
        self.assertEqual(state["applyId"], 333)
        self.assertEqual(state["payloadSha256"], "retained")

    def test_unique_technical_number_recovers_missing_receipt(self):
        self.lookup.return_value = {"productId": 222}
        self.assertTrue(batch.verify_submitted(self.folder, self.args, self.verifier)["ok"])
        self.lookup.assert_called_once_with("TEST-one")
        self.remote_write.assert_not_called()

    def test_lookup_absence_remains_unknown(self):
        with self.assertRaises(batch.VerificationPending):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.assertEqual(batch.read_state(self.folder)["stage"], "SUBMITTING")
        self.verifier.assert_not_called()
        self.remote_write.assert_not_called()

    def test_recovered_id_for_different_item_is_blocked(self):
        self.lookup.return_value = {"productId": 222}
        self.detail.return_value = {"data": {"productInfo": {"itemNum": "OTHER"}}}
        with self.assertRaises(batch.BatchStopError):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.verifier.assert_not_called()

    def test_changed_plan_cannot_reconcile_unknown_request(self):
        ab.mover.write_json(self.folder / "plan.json", {**self.plan, "itemNumber": "CHANGED"})
        with self.assertRaises(batch.BatchStopError):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.lookup.assert_not_called()

    def test_known_id_is_read_without_lookup_or_creation(self):
        ab.pipeline.save_state(self.folder / "state.json", "PUBLISHED", productId=222)
        batch.verify_submitted(self.folder, self.args, self.verifier)
        self.lookup.assert_not_called()
        self.remote_write.assert_not_called()

    def test_original_a_cannot_be_recovered_as_b(self):
        ab.pipeline.save_state(self.folder / "state.json", "PUBLISHED", productId=1)
        with self.assertRaises(batch.BatchStopError):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.verifier.assert_not_called()

    def test_rejected_application_is_not_recreated_or_reverified(self):
        ab.pipeline.save_state(self.folder / "state.json", "PRODUCT_APPLY_REJECTED", productId=222)
        with self.assertRaises(ValueError):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.verifier.assert_not_called()
        self.lookup.assert_not_called()

    def test_state_ids_survive_receipt_write_failure(self):
        write_json = batch.mover.write_json
        def write(path, value):
            if path.name == "response.json":
                raise PermissionError("receipt locked")
            write_json(path, value)
        with patch.object(batch.mover, "write_json", side_effect=write):
            with self.assertRaises(PermissionError):
                batch.record_response(self.folder, {"data": {"productId": 222, "applyId": 333}})
        self.assertEqual(batch.read_state(self.folder)["productId"], 222)
        self.assertEqual(batch.read_state(self.folder)["applyId"], 333)

    def test_receipt_survives_state_write_failure(self):
        with patch.object(batch.pipeline, "save_state", side_effect=PermissionError("state locked")):
            with self.assertRaises(PermissionError):
                batch.record_response(self.folder, {"data": {"productId": 222}})
        self.assertEqual(ab.mover.read_json(self.folder / "publish/response.json")["data"]["productId"], 222)

    def test_apply_id_alone_does_not_become_product_id(self):
        with self.assertRaises(batch.VerificationPending):
            batch.record_response(self.folder, {"data": {"applyId": 333}})
        state = batch.read_state(self.folder)
        self.assertEqual(state["applyId"], 333)
        self.assertNotIn("productId", state)

    def test_apply_only_rejection_is_confirmed_without_creating_product(self):
        ab.pipeline.save_state(self.folder / "state.json", "SUBMITTING", applyId=333)
        self.detail.return_value = {"data": {"applyInfo": {"applyId": 333, "draftComputeStatus": 3}}}
        with self.assertRaisesRegex(ValueError, "rejected"):
            batch.verify_submitted(self.folder, self.args, self.verifier)
        self.lookup.assert_not_called()
        self.verifier.assert_not_called()
        state = batch.read_state(self.folder)
        self.assertEqual(state["stage"], "PRODUCT_APPLY_REJECTED")
        self.assertEqual(state["applyId"], 333)
        self.assertNotIn("productId", state)

    def test_wrong_store_prevents_all_recovery_reads(self):
        with patch.object(batch.pipeline, "verify_store_identity", return_value={"profile": "wrong"}):
            with self.assertRaises(batch.BatchStopError):
                batch.verify_submitted(self.folder, self.args, self.verifier)
        self.lookup.assert_not_called()
        self.detail.assert_not_called()
        self.verifier.assert_not_called()

    def test_transient_verification_never_increments_failed_count(self):
        ab.pipeline.save_state(self.folder / "state.json", "PUBLISHED", productId=222)
        result = batch.error_result(self.folder, batch.VerificationPending("pending"), verifying=True)
        progress = {"requested": 1, "results": [result]}
        batch.finalize_progress(progress)
        self.assertEqual(progress["failed"], 0)
        self.assertEqual(progress["awaitingVerification"], 1)
        self.assertFalse(progress["completed"])

    def test_confirmed_audit_mismatch_is_failed_not_success(self):
        ab.pipeline.save_state(self.folder / "state.json", "PUBLISHED", productId=222)
        result = batch.error_result(self.folder, ValueError("price mismatch"), verifying=True)
        progress = {"requested": 1, "results": [result]}
        batch.finalize_progress(progress)
        self.assertEqual(progress["failed"], 1)
        self.assertEqual(result["productId"], 222)
        self.assertFalse(progress["ok"])


class PublishPhaseTests(unittest.TestCase):
    def fixture(self, module):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = {"profile": "test", "venderId": 10, "shopName": "test"}
        listing = {"images": [], "product": {}}
        ab.mover.write_json(folder / "listing.json", listing)
        ab.mover.write_json(folder / "listing.uploaded.json", listing)
        workbook = folder / "source.xlsx"
        workbook.write_bytes(b"synthetic-workbook")
        plan = {"sourceSku": "one", "store": store, "originalProductId": 1, "originalFingerprint": ab.original_fingerprint({}),
                "listingSha256": ab.file_hash(folder / "listing.json"), "files": {}, "itemNumber": "TEST-one",
                "merchantWorkbookPath": str(workbook), "merchantWorkbookSha256": ab.file_hash(workbook),
                "contentReview": {"decision": "PASS"}}
        ab.mover.write_json(folder / "plan.json", plan)
        ab.mover.write_json(folder / "dry-run.json", {"confirmToken": ab.digest_json(plan)})
        ab.mover.write_json(folder / "store.json", {})
        args = SimpleNamespace(confirm="PUBLISH_TO_JD", store_config=folder / "store.json", profile="test", reference=folder / "reference.json", output=folder)
        self.enterContext(patch.object(module.pipeline, "verify_store_identity", return_value=store))
        self.lookup = self.enterContext(patch.object(module.pipeline, "find_existing_product", return_value=None))
        self.enterContext(patch.object(ab, "get_detail", return_value={}))
        self.enterContext(patch.object(supplement, "update_shared_uploaded_images"))
        self.enterContext(patch.object(module.jingmai, "get", side_effect=AssertionError("No network")))
        self.uploader = self.enterContext(patch.object(module.jingmai, "command_upload", side_effect=AssertionError("No upload")))

        def payloads(listing_path, destination):
            ab.mover.write_json(destination / "publish-body.json", {"productApplyDTO": {}, "schema": "[]"})
            ab.mover.write_json(destination / "validation-body.json", {})
        self.enterContext(patch.object(module.jingmai, "write_publish_payloads", side_effect=payloads))
        self.remote = self.enterContext(patch.object(module.jingmai, "run_jdo_large", side_effect=[{}, {"data": {"productId": 222, "applyId": 333}}]))
        self.verifier = self.enterContext(patch.object(module, "verify_pair" if module is ab else "verify", return_value={"ok": True, "productId": 222}))
        return folder, args, module.publish_pair if module is ab else module.publish_one

    def test_real_publish_functions_save_ids_and_defer_verification(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__), contextlib.redirect_stdout(io.StringIO()):
                folder, args, publish = self.fixture(module)
                result = publish(folder, args, defer_verification=True)
                self.assertTrue(result["pending"])
                self.assertFalse(result["ok"])
                self.assertEqual(batch.read_state(folder)["productId"], 222)
                self.assertEqual(batch.read_state(folder)["applyId"], 333)
                self.assertEqual(self.remote.call_count, 2)
                self.verifier.assert_not_called()
                self.uploader.assert_not_called()

    def test_existing_id_and_unknown_state_never_submit_again(self):
        for module in (ab, supplement):
            for state in ({"stage": "PUBLISHED", "productId": 222}, {"stage": "SUBMITTING"}):
                with self.subTest(module=module.__name__, state=state):
                    folder, args, publish = self.fixture(module)
                    ab.mover.write_json(folder / "state.json", state)
                    self.assertTrue(publish(folder, args, defer_verification=True)["pending"])
                    self.remote.assert_not_called()
                    self.lookup.assert_not_called()

    def test_missing_confirmation_has_no_submission_side_effects(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                folder, args, publish = self.fixture(module)
                args.confirm = ""
                with self.assertRaises(ValueError):
                    publish(folder, args, defer_verification=True)
                self.remote.assert_not_called()
                self.uploader.assert_not_called()
                self.assertFalse((folder / "state.json").exists())

    def test_modified_listing_still_blocks_submission(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                folder, args, publish = self.fixture(module)
                ab.mover.write_json(folder / "listing.json", {"changed": True})
                with self.assertRaises(ValueError):
                    publish(folder, args, defer_verification=True)
                self.remote.assert_not_called()

    def test_verify_cli_dispatches_only_readback(self):
        for module in (ab, supplement):
            with self.subTest(module=module.__name__):
                runner_name = "run" if module is ab else "publish"
                with patch.object(sys, "argv", ["tool", "verify", "--output", "local", "--store-config", "store.json", "--profile", "test", "--reference", "reference.json"]), patch.object(module, runner_name) as runner:
                    module.main()
                self.assertTrue(runner.call_args.kwargs["audit_only"])
                self.assertEqual(runner.call_args.args[0].confirm, "")
                self.assertEqual(runner.call_args.args[0].limit, 0)


if __name__ == "__main__":
    unittest.main()
