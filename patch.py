with open("scripts/live_trader.py", "r") as f:
    lines = f.readlines()

# Find the second enter_position definition
enter_pos_lines = [i for i, l in enumerate(lines) if "async def enter_position(" in l]
exit_pos_lines = [i for i, l in enumerate(lines) if "async def exit_position(" in l]

print(f"enter_position definitions at lines: {enter_pos_lines}")
print(f"exit_position definitions at lines: {exit_pos_lines}")
