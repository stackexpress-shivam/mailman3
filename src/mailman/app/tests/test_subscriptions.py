# Copyright (C) 2011-2015 by the Free Software Foundation, Inc.
#
# This file is part of GNU Mailman.
#
# GNU Mailman is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# GNU Mailman is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# GNU Mailman.  If not, see <http://www.gnu.org/licenses/>.

"""Tests for the subscription service."""

__all__ = [
    'TestJoin',
    'TestSubscriptionWorkflow',
    ]


import uuid
import unittest

from mailman.app.lifecycle import create_list
from mailman.app.subscriptions import SubscriptionWorkflow
from mailman.interfaces.address import InvalidEmailAddressError
from mailman.interfaces.bans import IBanManager
from mailman.interfaces.member import (
    MemberRole, MembershipIsBannedError, MissingPreferredAddressError)
from mailman.interfaces.pending import IPendings
from mailman.interfaces.subscriptions import (
    MissingUserError, ISubscriptionService)
from mailman.testing.helpers import LogFileMark, get_queue_messages
from mailman.testing.layers import ConfigLayer
from mailman.interfaces.mailinglist import SubscriptionPolicy
from mailman.interfaces.usermanager import IUserManager
from mailman.utilities.datetime import now
from unittest.mock import patch
from zope.component import getUtility



class TestJoin(unittest.TestCase):
    layer = ConfigLayer

    def setUp(self):
        self._mlist = create_list('test@example.com')
        self._service = getUtility(ISubscriptionService)

    def test_join_user_with_bogus_id(self):
        # When `subscriber` is a missing user id, an exception is raised.
        with self.assertRaises(MissingUserError) as cm:
            self._service.join('test.example.com', uuid.UUID(int=99))
        self.assertEqual(cm.exception.user_id, uuid.UUID(int=99))

    def test_join_user_with_invalid_email_address(self):
        # When `subscriber` is a string that is not an email address, an
        # exception is raised.
        with self.assertRaises(InvalidEmailAddressError) as cm:
            self._service.join('test.example.com', 'bogus')
        self.assertEqual(cm.exception.email, 'bogus')

    def test_missing_preferred_address(self):
        # A user cannot join a mailing list if they have no preferred address.
        anne = self._service.join(
            'test.example.com', 'anne@example.com', 'Anne Person')
        # Try to join Anne as a user with a different role.  Her user has no
        # preferred address, so this will fail.
        self.assertRaises(MissingPreferredAddressError,
                          self._service.join,
                          'test.example.com', anne.user.user_id,
                          role=MemberRole.owner)



class TestSubscriptionWorkflow(unittest.TestCase):
    layer = ConfigLayer
    maxDiff = None

    def setUp(self):
        self._mlist = create_list('test@example.com')
        self._mlist.admin_immed_notify = False
        self._anne = 'anne@example.com'
        self._user_manager = getUtility(IUserManager)

    def test_user_or_address_required(self):
        # The `subscriber` attribute must be a user or address.
        workflow = SubscriptionWorkflow(self._mlist)
        self.assertRaises(AssertionError, list, workflow)

    def test_sanity_checks_address(self):
        # Ensure that the sanity check phase, when given an IAddress, ends up
        # with a linked user.
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        self.assertIsNotNone(workflow.address)
        self.assertIsNone(workflow.user)
        workflow.run_thru('sanity_checks')
        self.assertIsNotNone(workflow.address)
        self.assertIsNotNone(workflow.user)
        self.assertEqual(list(workflow.user.addresses)[0].email, self._anne)

    def test_sanity_checks_user_with_preferred_address(self):
        # Ensure that the sanity check phase, when given an IUser with a
        # preferred address, ends up with an address.
        anne = self._user_manager.make_user(self._anne)
        address = list(anne.addresses)[0]
        address.verified_on = now()
        anne.preferred_address = address
        workflow = SubscriptionWorkflow(self._mlist, anne)
        # The constructor sets workflow.address because the user has a
        # preferred address.
        self.assertEqual(workflow.address, address)
        self.assertEqual(workflow.user, anne)
        workflow.run_thru('sanity_checks')
        self.assertEqual(workflow.address, address)
        self.assertEqual(workflow.user, anne)

    def test_sanity_checks_user_without_preferred_address(self):
        # Ensure that the sanity check phase, when given a user without a
        # preferred address, but with at least one linked address, gets an
        # address.
        anne = self._user_manager.make_user(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        self.assertIsNone(workflow.address)
        self.assertEqual(workflow.user, anne)
        workflow.run_thru('sanity_checks')
        self.assertIsNotNone(workflow.address)
        self.assertEqual(workflow.user, anne)

    def test_sanity_checks_user_with_multiple_linked_addresses(self):
        # Ensure that the santiy check phase, when given a user without a
        # preferred address, but with multiple linked addresses, gets of of
        # those addresses (exactly which one is undefined).
        anne = self._user_manager.make_user(self._anne)
        anne.link(self._user_manager.create_address('anne@example.net'))
        anne.link(self._user_manager.create_address('anne@example.org'))
        workflow = SubscriptionWorkflow(self._mlist, anne)
        self.assertIsNone(workflow.address)
        self.assertEqual(workflow.user, anne)
        workflow.run_thru('sanity_checks')
        self.assertIn(workflow.address.email, ['anne@example.com',
                                               'anne@example.net',
                                               'anne@example.org'])
        self.assertEqual(workflow.user, anne)

    def test_sanity_checks_user_without_addresses(self):
        # It is an error to try to subscribe a user with no linked addresses.
        user = self._user_manager.create_user()
        workflow = SubscriptionWorkflow(self._mlist, user)
        self.assertRaises(AssertionError, workflow.run_thru, 'sanity_checks')

    def test_sanity_checks_globally_banned_address(self):
        # An exception is raised if the address is globally banned.
        anne = self._user_manager.create_address(self._anne)
        IBanManager(None).ban(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        self.assertRaises(MembershipIsBannedError, list, workflow)

    def test_sanity_checks_banned_address(self):
        # An exception is raised if the address is banned by the mailing list.
        anne = self._user_manager.create_address(self._anne)
        IBanManager(self._mlist).ban(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        self.assertRaises(MembershipIsBannedError, list, workflow)

    def test_verification_checks_with_verified_address(self):
        # When the address is already verified, we skip straight to the
        # confirmation checks.
        anne = self._user_manager.create_address(self._anne)
        anne.verified_on = now()
        workflow = SubscriptionWorkflow(self._mlist, anne)
        workflow.run_thru('verification_checks')
        with patch.object(workflow, '_step_confirmation_checks') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_verification_checks_with_pre_verified_address(self):
        # When the address is not yet verified, but the pre-verified flag is
        # passed to the workflow, we skip to the confirmation checks.
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('verification_checks')
        with patch.object(workflow, '_step_confirmation_checks') as step:
            next(workflow)
        step.assert_called_once_with()
        # And now the address is verified.
        self.assertIsNotNone(anne.verified_on)

    def test_verification_checks_confirmation_needed(self):
        # The address is neither verified, nor is the pre-verified flag set.
        # A confirmation message must be sent to the user which will also
        # verify their address.
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        workflow.run_thru('verification_checks')
        with patch.object(workflow, '_step_send_confirmation') as step:
            next(workflow)
        step.assert_called_once_with()
        # The address still hasn't been verified.
        self.assertIsNone(anne.verified_on)

    def test_confirmation_checks_open_list(self):
        # A subscription to an open list does not need to be confirmed or
        # moderated.
        self._mlist.subscription_policy = SubscriptionPolicy.open
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_do_subscription') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_confirmation_checks_no_user_confirmation_needed(self):
        # A subscription to a list which does not need user confirmation skips
        # to the moderation checks.
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_moderation_checks') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_confirmation_checks_confirm_pre_confirmed(self):
        # The subscription policy requires user confirmation, but their
        # subscription is pre-confirmed.
        self._mlist.subscription_policy = SubscriptionPolicy.confirm
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_moderation_checks') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_confirmation_checks_confirm_and_moderate_pre_confirmed(self):
        # The subscription policy requires user confirmation and moderation,
        # but their subscription is pre-confirmed.
        self._mlist.subscription_policy = \
          SubscriptionPolicy.confirm_then_moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_moderation_checks') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_confirmation_checks_confirmation_needed(self):
        # The subscription policy requires confirmation and the subscription
        # is not pre-confirmed.
        self._mlist.subscription_policy = SubscriptionPolicy.confirm
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_send_confirmation') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_confirmation_checks_moderate_confirmation_needed(self):
        # The subscription policy requires confirmation and moderation, and the
        # subscription is not pre-confirmed.
        self._mlist.subscription_policy = \
          SubscriptionPolicy.confirm_then_moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('confirmation_checks')
        with patch.object(workflow, '_step_send_confirmation') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_moderation_checks_pre_approved(self):
        # The subscription is pre-approved by the moderator.
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_approved=True)
        workflow.run_thru('moderation_checks')
        with patch.object(workflow, '_step_do_subscription') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_moderation_checks_approval_required(self):
        # The moderator must approve the subscription.
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        workflow.run_thru('moderation_checks')
        with patch.object(workflow, '_step_get_moderator_approval') as step:
            next(workflow)
        step.assert_called_once_with()

    def test_do_subscription(self):
        # An open subscription policy plus a pre-verified address means the
        # user gets subscribed to the mailing list without any further
        # confirmations or approvals.
        self._mlist.subscription_policy = SubscriptionPolicy.open
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        # Consume the entire state machine.
        list(workflow)
        # Anne is now a member of the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertEqual(member.address, anne)

    def test_do_subscription_pre_approved(self):
        # An moderation-requiring subscription policy plus a pre-verified and
        # pre-approved address means the user gets subscribed to the mailing
        # list without any further confirmations or approvals.
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_approved=True)
        # Consume the entire state machine.
        list(workflow)
        # Anne is now a member of the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertEqual(member.address, anne)

    def test_do_subscription_pre_approved_pre_confirmed(self):
        # An moderation-requiring subscription policy plus a pre-verified and
        # pre-approved address means the user gets subscribed to the mailing
        # list without any further confirmations or approvals.
        self._mlist.subscription_policy = \
          SubscriptionPolicy.confirm_then_moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True,
                                        pre_approved=True)
        # Consume the entire state machine.
        list(workflow)
        # Anne is now a member of the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertEqual(member.address, anne)

    def test_do_subscription_cleanups(self):
        # Once the user is subscribed, the token, and its associated pending
        # database record will be removed from the database.
        self._mlist.subscription_policy = SubscriptionPolicy.open
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True,
                                        pre_approved=True)
        # Cache the token.
        token = workflow.token
        # Consume the entire state machine.
        list(workflow)
        # Anne is now a member of the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertEqual(member.address, anne)
        # The workflow is done, so it has no token.
        self.assertIsNone(workflow.token)
        # The pendable associated with the token has been evicted.
        self.assertIsNone(getUtility(IPendings).confirm(token, expunge=False))
        # There is no saved workflow associated with the token.  This shows up
        # as an exception when we try to restore the workflow.
        new_workflow = SubscriptionWorkflow(self._mlist)
        new_workflow.token = token
        self.assertRaises(LookupError, new_workflow.restore)

    def test_moderator_approves(self):
        # The workflow runs until moderator approval is required, at which
        # point the workflow is saved.  Once the moderator approves, the
        # workflow resumes and the user is subscribed.
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        # Consume the entire state machine.
        list(workflow)
        # The user is not currently subscribed to the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertIsNone(member)
        # Create a new workflow with the previous workflow's save token, and
        # restore its state.  This models an approved subscription and should
        # result in the user getting subscribed.
        approved_workflow = SubscriptionWorkflow(self._mlist)
        approved_workflow.token = workflow.token
        approved_workflow.restore()
        list(approved_workflow)
        # Now the user is subscribed to the mailing list.
        member = self._mlist.regular_members.get_member(self._anne)
        self.assertEqual(member.address, anne)

    def test_get_moderator_approval_log_on_hold(self):
        # When the subscription is held for moderator approval, a message is
        # logged.
        mark = LogFileMark('mailman.subscribe')
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        # Consume the entire state machine.
        list(workflow)
        line = mark.readline()
        self.assertEqual(
            line[29:-1],
            'test@example.com: held subscription request from anne@example.com'
            )

    def test_get_moderator_approval_notifies_moderators(self):
        # When the subscription is held for moderator approval, and the list
        # is so configured, a notification is sent to the list moderators.
        self._mlist.admin_immed_notify = True
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        # Consume the entire state machine.
        list(workflow)
        items = get_queue_messages('virgin')
        self.assertEqual(len(items), 1)
        message = items[0].msg
        self.assertEqual(message['From'], 'test-owner@example.com')
        self.assertEqual(message['To'], 'test-owner@example.com')
        self.assertEqual(
            message['Subject'],
            'New subscription request to Test from anne@example.com')
        self.assertEqual(message.get_payload(), """\
Your authorization is required for a mailing list subscription request
approval:

    For:  anne@example.com
    List: test@example.com""")

    def test_get_moderator_approval_no_notifications(self):
        # When the subscription is held for moderator approval, and the list
        # is so configured, a notification is sent to the list moderators.
        self._mlist.admin_immed_notify = False
        self._mlist.subscription_policy = SubscriptionPolicy.moderate
        anne = self._user_manager.create_address(self._anne)
        workflow = SubscriptionWorkflow(self._mlist, anne,
                                        pre_verified=True,
                                        pre_confirmed=True)
        # Consume the entire state machine.
        list(workflow)
        items = get_queue_messages('virgin')
        self.assertEqual(len(items), 0)

    def test_send_confirmation(self):
        # A confirmation message gets sent when the address is not verified.
        anne = self._user_manager.create_address(self._anne)
        self.assertIsNone(anne.verified_on)
        # Run the workflow to model the confirmation step.
        workflow = SubscriptionWorkflow(self._mlist, anne)
        list(workflow)
        items = get_queue_messages('virgin')
        self.assertEqual(len(items), 1)
        message = items[0].msg
        token = workflow.token
        self.assertEqual(message['Subject'], 'confirm {}'.format(token))
        self.assertEqual(
            message['From'], 'test-confirm+{}@example.com'.format(token))

    def test_send_confirmation_pre_confirmed(self):
        # A confirmation message gets sent when the address is not verified
        # but the subscription is pre-confirmed.
        anne = self._user_manager.create_address(self._anne)
        self.assertIsNone(anne.verified_on)
        # Run the workflow to model the confirmation step.
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_confirmed=True)
        list(workflow)
        items = get_queue_messages('virgin')
        self.assertEqual(len(items), 1)
        message = items[0].msg
        token = workflow.token
        self.assertEqual(
            message['Subject'], 'confirm {}'.format(workflow.token))
        self.assertEqual(
            message['From'], 'test-confirm+{}@example.com'.format(token))

    def test_send_confirmation_pre_verified(self):
        # A confirmation message gets sent even when the address is verified
        # when the subscription must be confirmed.
        self._mlist.subscription_policy = SubscriptionPolicy.confirm
        anne = self._user_manager.create_address(self._anne)
        self.assertIsNone(anne.verified_on)
        # Run the workflow to model the confirmation step.
        workflow = SubscriptionWorkflow(self._mlist, anne, pre_verified=True)
        list(workflow)
        items = get_queue_messages('virgin')
        self.assertEqual(len(items), 1)
        message = items[0].msg
        token = workflow.token
        self.assertEqual(
            message['Subject'], 'confirm {}'.format(workflow.token))
        self.assertEqual(
            message['From'], 'test-confirm+{}@example.com'.format(token))

    def test_do_confirm_verify_address(self):
        # The address is not yet verified, nor are we pre-verifying.  A
        # confirmation message will be sent.  When the user confirms their
        # subscription request, the address will end up being verified.
        anne = self._user_manager.create_address(self._anne)
        self.assertIsNone(anne.verified_on)
        # Run the workflow to model the confirmation step.
        workflow = SubscriptionWorkflow(self._mlist, anne)
        list(workflow)
        # The address is still not verified.
        self.assertIsNone(anne.verified_on)
        confirm_workflow = SubscriptionWorkflow(self._mlist)
        confirm_workflow.token = workflow.token
        confirm_workflow.restore()
        confirm_workflow.run_thru('do_confirm_verify')
        # The address is now verified.
        self.assertIsNotNone(anne.verified_on)

    def test_do_confirmation_subscribes_user(self):
        # Subscriptions to the mailing list must be confirmed.  Once that's
        # done, the user's address (which is not initially verified) gets
        # subscribed to the mailing list.
        self._mlist.subscription_policy = SubscriptionPolicy.confirm
        anne = self._user_manager.create_address(self._anne)
        self.assertIsNone(anne.verified_on)
        workflow = SubscriptionWorkflow(self._mlist, anne)
        list(workflow)
        self.assertIsNone(self._mlist.regular_members.get_member(self._anne))
        confirm_workflow = SubscriptionWorkflow(self._mlist)
        confirm_workflow.token = workflow.token
        confirm_workflow.restore()
        list(confirm_workflow)
        self.assertIsNotNone(anne.verified_on)
        self.assertEqual(
            self._mlist.regular_members.get_member(self._anne).address, anne)
