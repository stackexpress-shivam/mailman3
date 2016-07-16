# Copyright (C) 2007-2016 by the Free Software Foundation, Inc.
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

"""Implementation of the IRegistrar interface."""

import logging

from mailman import public
from mailman.app.subscriptions import SubscriptionWorkflow
from mailman.database.transaction import flush
from mailman.email.message import UserNotification
from mailman.interfaces.pending import IPendable, IPendings
from mailman.interfaces.registrar import ConfirmationNeededEvent, IRegistrar
from mailman.interfaces.template import ITemplateLoader
from mailman.interfaces.workflow import IWorkflowStateManager
from mailman.utilities.string import expand
from zope.component import getUtility
from zope.interface import implementer


log = logging.getLogger('mailman.error')


@implementer(IPendable)
class PendableRegistration(dict):
    PEND_TYPE = 'registration'


@public
@implementer(IRegistrar)
class Registrar:
    """Handle registrations and confirmations for subscriptions."""

    def __init__(self, mlist):
        self._mlist = mlist

    def register(self, subscriber=None, *,
                 pre_verified=False, pre_confirmed=False, pre_approved=False):
        """See `IRegistrar`."""
        workflow = SubscriptionWorkflow(
            self._mlist, subscriber,
            pre_verified=pre_verified,
            pre_confirmed=pre_confirmed,
            pre_approved=pre_approved)
        list(workflow)
        return workflow.token, workflow.token_owner, workflow.member

    def confirm(self, token):
        """See `IRegistrar`."""
        workflow = SubscriptionWorkflow(self._mlist)
        workflow.token = token
        workflow.restore()
        list(workflow)
        return workflow.token, workflow.token_owner, workflow.member

    def discard(self, token):
        """See `IRegistrar`."""
        with flush():
            getUtility(IPendings).confirm(token)
            getUtility(IWorkflowStateManager).discard(
                SubscriptionWorkflow.__name__, token)


@public
def handle_ConfirmationNeededEvent(event):
    if not isinstance(event, ConfirmationNeededEvent):
        return
    # There are three ways for a user to confirm their subscription.  They
    # can reply to the original message and let the VERP'd return address
    # encode the token, they can reply to the robot and keep the token in
    # the Subject header, or they can click on the URL in the body of the
    # message and confirm through the web.
    subject = 'confirm {}'.format(event.token)
    confirm_address = event.mlist.confirm_address(event.token)
    email_address = event.email
    # Send a verification email to the address.
    template = getUtility(ITemplateLoader).get(
        'list:user:action:confirm', event.mlist)
    text = expand(template, event.mlist, dict(
        token=event.token,
        subject=subject,
        confirm_email=confirm_address,
        user_email=email_address,
        # For backward compatibility.
        confirm_address=confirm_address,
        email_address=email_address,
        domain_name=event.mlist.domain.mail_host,
        contact_address=event.mlist.owner_address,
        ))
    msg = UserNotification(email_address, confirm_address, subject, text)
    msg.send(event.mlist, add_precedence=False)
