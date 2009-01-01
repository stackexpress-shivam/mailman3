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

"""Parse bounce messages generated by qmail.

Qmail actually has a standard, called QSBMF (qmail-send bounce message
format), as described in

    http://cr.yp.to/proto/qsbmf.txt

This module should be conformant.

"""

import re
import email.Iterators

# Other (non-standard?) intros have been observed in the wild.
introtags = [
    'Hi. This is the',
    "We're sorry. There's a problem",
    'Check your send e-mail address.',
    'This is the mail delivery agent at',
    'Unfortunately, your mail was not delivered'
    ]
acre = re.compile(r'<(?P<addr>[^>]*)>:')



def process(msg):
    addrs = []
    # simple state machine
    #    0 = nothing seen yet
    #    1 = intro paragraph seen
    #    2 = recip paragraphs seen
    state = 0
    for line in email.Iterators.body_line_iterator(msg):
        line = line.strip()
        if state == 0:
            for introtag in introtags:
                if line.startswith(introtag):
                    state = 1
                    break
        elif state == 1 and not line:
            # Looking for the end of the intro paragraph
            state = 2
        elif state == 2:
            if line.startswith('-'):
                # We're looking at the break paragraph, so we're done
                break
            # At this point we know we must be looking at a recipient
            # paragraph
            mo = acre.match(line)
            if mo:
                addrs.append(mo.group('addr'))
            # Otherwise, it must be a continuation line, so just ignore it
        # Not looking at anything in particular
    return addrs
