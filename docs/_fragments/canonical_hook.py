from captain_hook import gate, TouchedFile, UsedSkill

# A Stop gate: before the agent finishes, block if it edited UI files
# without doing a visual review.
gate(
    # the one-line reason shown to the agent when the gate fires
    "You edited UI files — open them with agent-browser and verify they render before finishing.",
    only_if=[TouchedFile("**/src/routes/**", "**/src/components/**")],  # fires only if UI files changed
    skip_if=[UsedSkill("agent-browser")],                               # already reviewed -> don't block
)
