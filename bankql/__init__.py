"""BankQL — local-LLM text-to-SQL agent for bank data.

    from bankql import Agent
    agent = Agent.from_config("config/")
    result = agent.ask("show client 12's accounts", source="financial", session_id="s1")
"""
__all__ = ["Agent", "AskResult"]


def __getattr__(name):  # lazy: `import bankql` stays cheap
    if name in __all__:
        from bankql import agent

        return getattr(agent, name)
    raise AttributeError(name)
