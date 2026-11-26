def greet(greeting: str, name: str, times: int) -> str:
    return " ".join([f"{greeting}, {name}!"] * int(times))
