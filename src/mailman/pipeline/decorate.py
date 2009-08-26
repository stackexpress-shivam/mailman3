# Copyright (C) 1998-2009 by the Free Software Foundation, Inc.
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

"""Decorate a message by sticking the header and footer around it."""

from __future__ import absolute_import, unicode_literals

__metaclass__ = type
__all__ = [
    'Decorate',
    ]


import re
import logging

from email.MIMEText import MIMEText
from zope.component import getUtility
from zope.interface import implements

from mailman.config import config
from mailman.email.message import Message
from mailman.i18n import _
from mailman.interfaces.handler import IHandler
from mailman.interfaces.usermanager import IUserManager
from mailman.utilities.string import expand


log = logging.getLogger('mailman.error')



def process(mlist, msg, msgdata):
    # Digests and Mailman-craft messages should not get additional headers
    if msgdata.get('isdigest') or msgdata.get('nodecorate'):
        return
    d = {}
    if msgdata.get('personalize'):
        # Calculate the extra personalization dictionary.  Note that the
        # length of the recips list better be exactly 1.
        recips = msgdata.get('recips', [])
        assert len(recips) == 1, (
            'The number of intended recipients must be exactly 1')
        recipient = recips[0].lower()
        user = getUtility(IUserManager).get_user(recipient)
        member = mlist.members.get_member(recipient)
        d['user_address'] = recipient
        if user is not None and member is not None:
            d['user_delivered_to'] = member.address.original_address
            # BAW: Hmm, should we allow this?
            d['user_password'] = user.password
            d['user_language'] = member.preferred_language
            d['user_name'] = (user.real_name if user.real_name
                              else member.address.original_address)
            d['user_optionsurl'] = member.options_url
    # These strings are descriptive for the log file and shouldn't be i18n'd
    d.update(msgdata.get('decoration-data', {}))
    header = decorate(mlist, mlist.msg_header, d)
    footer = decorate(mlist, mlist.msg_footer, d)
    # Escape hatch if both the footer and header are empty
    if not header and not footer:
        return
    # Be MIME smart here.  We only attach the header and footer by
    # concatenation when the message is a non-multipart of type text/plain.
    # Otherwise, if it is not a multipart, we make it a multipart, and then we
    # add the header and footer as text/plain parts.
    #
    # BJG: In addition, only add the footer if the message's character set
    # matches the charset of the list's preferred language.  This is a
    # suboptimal solution, and should be solved by allowing a list to have
    # multiple headers/footers, for each language the list supports.
    #
    # Also, if the list's preferred charset is us-ascii, we can always
    # safely add the header/footer to a plain text message since all
    # charsets Mailman supports are strict supersets of us-ascii --
    # no, UTF-16 emails are not supported yet.
    #
    # TK: Message with 'charset=' cause trouble. So, instead of
    #     mgs.get_content_charset('us-ascii') ...
    mcset = msg.get_content_charset() or 'us-ascii'
    lcset = mlist.preferred_language.charset
    msgtype = msg.get_content_type()
    # BAW: If the charsets don't match, should we add the header and footer by
    # MIME multipart chroming the message?
    wrap = True
    if not msg.is_multipart() and msgtype == 'text/plain':
        # Save the RFC-3676 format parameters.
        format = msg.get_param('format')
        delsp = msg.get_param('delsp')
        # Save 'Content-Transfer-Encoding' header in case decoration fails.
        cte = msg.get('content-transfer-encoding')
        # header/footer is now in unicode (2.2)
        try:
            oldpayload = unicode(msg.get_payload(decode=True), mcset)
            del msg['content-transfer-encoding']
            frontsep = endsep = ''
            if header and not header.endswith('\n'):
                frontsep = '\n'
            if footer and not oldpayload.endswith('\n'):
                endsep = '\n'
            payload = header + frontsep + oldpayload + endsep + footer
            # When setting the payload for the message, try various charset
            # encodings until one does not produce a UnicodeError.  We'll try
            # charsets in this order: the list's charset, the message's
            # charset, then utf-8.  It's okay if some of these are duplicates.
            for cset in (lcset, mcset, 'utf-8'):
                try:
                    msg.set_payload(payload.encode(cset), cset)
                except UnicodeError:
                    pass
                else:
                    if format:
                        msg.set_param('format', format)
                    if delsp:
                        msg.set_param('delsp', delsp)
                    wrap = False
                    break
        except (LookupError, UnicodeError):
            if cte:
                # Restore the original c-t-e.
                del msg['content-transfer-encoding']
                msg['Content-Transfer-Encoding'] = cte
    elif msg.get_content_type() == 'multipart/mixed':
        # The next easiest thing to do is just prepend the header and append
        # the footer as additional subparts
        payload = msg.get_payload()
        if not isinstance(payload, list):
            payload = [payload]
        if footer:
            mimeftr = MIMEText(footer.encode(lcset), 'plain', lcset)
            mimeftr['Content-Disposition'] = 'inline'
            payload.append(mimeftr)
        if header:
            mimehdr = MIMEText(header.encode(lcset), 'plain', lcset)
            mimehdr['Content-Disposition'] = 'inline'
            payload.insert(0, mimehdr)
        msg.set_payload(payload)
        wrap = False
    # If we couldn't add the header or footer in a less intrusive way, we can
    # at least do it by MIME encapsulation.  We want to keep as much of the
    # outer chrome as possible.
    if not wrap:
        return
    # Because of the way Message objects are passed around to process(), we
    # need to play tricks with the outer message -- i.e. the outer one must
    # remain the same instance.  So we're going to create a clone of the outer
    # message, with all the header chrome intact, then copy the payload to it.
    # This will give us a clone of the original message, and it will form the
    # basis of the interior, wrapped Message.
    inner = Message()
    # Which headers to copy?  Let's just do the Content-* headers
    for h, v in msg.items():
        if h.lower().startswith('content-'):
            inner[h] = v
    inner.set_payload(msg.get_payload())
    # For completeness
    inner.set_unixfrom(msg.get_unixfrom())
    inner.preamble = msg.preamble
    inner.epilogue = msg.epilogue
    # Don't copy get_charset, as this might be None, even if
    # get_content_charset isn't.  However, do make sure there is a default
    # content-type, even if the original message was not MIME.
    inner.set_default_type(msg.get_default_type())
    # BAW: HACK ALERT.
    if hasattr(msg, '__version__'):
        inner.__version__ = msg.__version__
    # Now, play games with the outer message to make it contain three
    # subparts: the header (if any), the wrapped message, and the footer (if
    # any).
    payload = [inner]
    if header:
        mimehdr = MIMEText(header.encode(lcset), 'plain', lcset)
        mimehdr['Content-Disposition'] = 'inline'
        payload.insert(0, mimehdr)
    if footer:
        mimeftr = MIMEText(footer.encode(lcset), 'plain', lcset)
        mimeftr['Content-Disposition'] = 'inline'
        payload.append(mimeftr)
    msg.set_payload(payload)
    del msg['content-type']
    del msg['content-transfer-encoding']
    del msg['content-disposition']
    msg['Content-Type'] = 'multipart/mixed'



def decorate(mlist, template, extradict=None):
    # Create a dictionary which includes the default set of interpolation
    # variables allowed in headers and footers.  These will be augmented by
    # any key/value pairs in the extradict.
    substitutions = dict(
        real_name       = mlist.real_name,
        list_name       = mlist.list_name,
        fqdn_listname   = mlist.fqdn_listname,
        host_name       = mlist.host_name,
        listinfo_page   = mlist.script_url('listinfo'),
        description     = mlist.description,
        info            = mlist.info,
        )
    if extradict is not None:
        substitutions.update(extradict)
    text = expand(template, substitutions)
    # Turn any \r\n line endings into just \n
    return re.sub(r' *\r?\n', r'\n', text)



class Decorate:
    """Decorate a message with headers and footers."""

    implements(IHandler)

    name = 'decorate'
    description = _('Decorate a message with headers and footers.')

    def process(self, mlist, msg, msgdata):
        "See `IHandler`."""
        process(mlist, msg, msgdata)
