# One Stop Sign-in Helper

Runs a hidden web browser inside Home Assistant so a Netflix account can be connected from the Home Assistant app on a phone: type the email, then the code Netflix sends. Nothing is needed on a computer.

The helper only talks to Home Assistant on the internal add-on network; it has no open ports. Sign-in sessions are discarded after 15 minutes. After connecting, the **One Stop Netflix history** integration keeps the sign-in.

Netflix has no public API. This uses Netflix's normal website sign-in, which Netflix can change or refuse at any time.
