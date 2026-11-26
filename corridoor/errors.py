"""Exceptions shared by all modules."""


class StepError(Exception):
    """A connection step failed; the message explains why.

    The message is shown to the user as is, so it should say what to check.
    """


class Cancelled(Exception):
    """The user asked to disconnect or to cancel the running job.

    This is a normal way to leave a session, not an error.
    """
