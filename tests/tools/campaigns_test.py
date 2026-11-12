# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the campaign write tools."""

import json
import unittest
from unittest.mock import MagicMock, create_autospec, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.campaign_budget_service import (
    CampaignBudgetServiceClient,
)
from google.ads.googleads.v25.services.services.campaign_service import (
    CampaignServiceClient,
)
from google.ads.googleads.v25.services.services.google_ads_service import (
    GoogleAdsServiceClient,
)
from google.auth.credentials import AnonymousCredentials

from ads_mcp.tools import campaigns

CID = "1234567890"


class TestCampaignTools(unittest.TestCase):
    """Covers propose (validate_only) and confirm for each write tool."""

    @classmethod
    def setUpClass(cls):
        cls.real = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        self.client = MagicMock()
        self.client.get_type.side_effect = self.real.get_type
        self.client.enums = self.real.enums

        self.ga = create_autospec(GoogleAdsServiceClient, instance=True)
        self.camp = create_autospec(CampaignServiceClient, instance=True)
        self.budget = create_autospec(
            CampaignBudgetServiceClient, instance=True
        )
        services = {
            "GoogleAdsService": self.ga,
            "CampaignService": self.camp,
            "CampaignBudgetService": self.budget,
        }
        self.client.get_service.side_effect = lambda n, **kw: services[n]

        row = self.real.get_type("GoogleAdsRow")
        row.campaign.id = 42
        row.campaign.name = "Sommarkampanj"
        row.campaign.status = self.real.enums.CampaignStatusEnum.ENABLED
        row.campaign_budget.resource_name = f"customers/{CID}/campaignBudgets/7"
        row.campaign_budget.amount_micros = 100_000_000
        row.campaign_budget.explicitly_shared = False
        row.customer.currency_code = "SEK"
        self.row = row
        self.ga.search.return_value = [row]

        patcher = patch("ads_mcp.utils.get_googleads_client")
        self.addCleanup(patcher.stop)
        patcher.start().return_value = self.client

    # --- set_campaign_status -------------------------------------------

    def test_pause_propose_then_commit(self):
        res = json.loads(
            campaigns.set_campaign_status(CID, "42", status="PAUSED")
        )
        self.assertEqual(res["status"], "PROPOSED")
        item = res["change_items"][0]
        self.assertEqual(item["value_readable"], "PAUSED")
        self.assertEqual(item["original_value_readable"], "ENABLED")

        req = self.camp.mutate_campaigns.call_args.kwargs["request"]
        self.assertTrue(req.validate_only)
        self.assertEqual(list(req.operations[0].update_mask.paths), ["status"])
        self.assertEqual(
            req.operations[0].update.status,
            self.real.enums.CampaignStatusEnum.PAUSED,
        )

        out = campaigns.set_campaign_status(
            CID,
            "42",
            status="PAUSED",
            validate_only=False,
            checksum=res["checksum"],
        )
        self.assertIn("paused", out)
        req = self.camp.mutate_campaigns.call_args.kwargs["request"]
        self.assertFalse(req.validate_only)
        self.assertEqual(self.camp.mutate_campaigns.call_count, 2)

    def test_commit_without_checksum_is_refused(self):
        with self.assertRaises(ToolError):
            campaigns.set_campaign_status(
                CID, "42", status="ENABLED", validate_only=False
            )
        self.camp.mutate_campaigns.assert_not_called()

    def test_checksum_from_other_params_is_refused(self):
        res = json.loads(
            campaigns.set_campaign_status(CID, "42", status="PAUSED")
        )
        with self.assertRaises(ToolError):
            campaigns.set_campaign_status(
                CID,
                "42",
                status="ENABLED",
                validate_only=False,
                checksum=res["checksum"],
            )
        self.assertEqual(self.camp.mutate_campaigns.call_count, 1)

    def test_unknown_campaign(self):
        self.ga.search.return_value = []
        with self.assertRaises(ToolError):
            campaigns.set_campaign_status(CID, "99", status="PAUSED")

    # --- update_campaign_daily_budget ----------------------------------

    def test_budget_propose_then_commit(self):
        res = json.loads(
            campaigns.update_campaign_daily_budget(CID, "42", daily_budget=250)
        )
        item = res["change_items"][0]
        self.assertEqual(item["value_readable"], "250.00 SEK")
        self.assertEqual(item["original_value_readable"], "100.00 SEK")
        req = self.budget.mutate_campaign_budgets.call_args.kwargs["request"]
        self.assertTrue(req.validate_only)
        self.assertEqual(req.operations[0].update.amount_micros, 250_000_000)
        self.assertEqual(
            req.operations[0].update.resource_name,
            f"customers/{CID}/campaignBudgets/7",
        )

        out = campaigns.update_campaign_daily_budget(
            CID,
            "42",
            daily_budget=250,
            validate_only=False,
            checksum=res["checksum"],
        )
        self.assertIn("250.00 SEK", out)
        req = self.budget.mutate_campaign_budgets.call_args.kwargs["request"]
        self.assertFalse(req.validate_only)

    def test_budget_rounds_to_cents(self):
        campaigns.update_campaign_daily_budget(CID, "42", daily_budget=99.999)
        req = self.budget.mutate_campaign_budgets.call_args.kwargs["request"]
        self.assertEqual(req.operations[0].update.amount_micros, 100_000_000)

    def test_shared_budget_requires_opt_in(self):
        self.row.campaign_budget.explicitly_shared = True
        self.row.campaign_budget.reference_count = 3
        with self.assertRaises(ToolError):
            campaigns.update_campaign_daily_budget(CID, "42", daily_budget=50)
        self.budget.mutate_campaign_budgets.assert_not_called()
        res = json.loads(
            campaigns.update_campaign_daily_budget(
                CID, "42", daily_budget=50, allow_shared_budget=True
            )
        )
        self.assertEqual(res["status"], "PROPOSED")

    def test_budget_must_be_positive(self):
        with self.assertRaises(ToolError):
            campaigns.update_campaign_daily_budget(CID, "42", daily_budget=0)

    # --- create_paused_campaign ----------------------------------------

    def test_create_propose_then_commit(self):
        res = json.loads(
            campaigns.create_paused_campaign(
                CID, name="Vinter 2026", daily_budget=150, max_cpc=12.5
            )
        )
        self.assertEqual(res["status"], "PROPOSED")
        fields = {
            i["field_readable"]: i["value_readable"]
            for i in res["change_items"]
        }
        self.assertEqual(fields["Status"], "PAUSED")
        self.assertEqual(fields["Daily budget"], "150.00")

        req = self.ga.mutate.call_args.kwargs["request"]
        self.assertTrue(req.validate_only)
        budget_op, camp_op = req.mutate_operations
        self.assertEqual(
            budget_op.campaign_budget_operation.create.amount_micros,
            150_000_000,
        )
        c = camp_op.campaign_operation.create
        self.assertEqual(c.status, self.real.enums.CampaignStatusEnum.PAUSED)
        self.assertEqual(
            c.campaign_budget, f"customers/{CID}/campaignBudgets/-1"
        )
        self.assertEqual(c.target_spend.cpc_bid_ceiling_micros, 12_500_000)
        self.assertFalse(c.network_settings.target_content_network)

        resp = MagicMock()
        r1, r2 = MagicMock(), MagicMock()
        r1.campaign_result.resource_name = ""
        r2.campaign_result.resource_name = f"customers/{CID}/campaigns/555"
        resp.mutate_operation_responses = [r1, r2]
        self.ga.mutate.return_value = resp

        out = campaigns.create_paused_campaign(
            CID,
            name="Vinter 2026",
            daily_budget=150,
            max_cpc=12.5,
            validate_only=False,
            checksum=res["checksum"],
        )
        self.assertIn("ID: 555", out)
        self.assertFalse(
            self.ga.mutate.call_args.kwargs["request"].validate_only
        )

    def test_create_max_conversions_selects_strategy(self):
        campaigns.create_paused_campaign(
            CID,
            name="X",
            daily_budget=10,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
        )
        c = (
            self.ga.mutate.call_args.kwargs["request"]
            .mutate_operations[1]
            .campaign_operation.create
        )
        self.assertEqual(
            type(c).pb(c).WhichOneof("campaign_bidding_strategy"),
            "maximize_conversions",
        )

    def test_max_cpc_only_with_maximize_clicks(self):
        with self.assertRaises(ToolError):
            campaigns.create_paused_campaign(
                CID,
                name="X",
                daily_budget=10,
                bidding_strategy="MANUAL_CPC",
                max_cpc=5,
            )


if __name__ == "__main__":
    unittest.main()
