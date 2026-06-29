"""
Plan-then-execute against a local Ollama server.
Requires a recent Ollama (the `think` param needs ~0.9+).
"""
 
from itertools import count

import requests
import re
 
URL = "http://192.168.1.134:11434"
 
# Standing instructions for the planner (goes in the system message).
PLANNER_SYSTEM = """You are a planning assistant. Produce a concrete, actionable plan.  
Given the user's goal and situation, return:
1. A plan broken into sequenced phases/milestones — what comes before what, and why.
2. For each phase: the concrete actions, and what "done" looks like before moving on.
3. The critical path — the few actions that actually drive the outcome vs. the optional ones.
4. The riskiest assumptions, and the earliest cheap way to test each.
 
Where details are missing, make a reasonable assumption and label it. Do not ask questions back."""
 
# Standing instructions for the executor.
EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output (the actual code/draft/artifact). Do not re-plan or describe what you
would do.
 
Where a step needs a fact or action you don't have, make the most reasonable assumption, label
it, and keep going. Check your output against the goal before finishing.
 
End with a short report:
- DONE: what you produced.
- STATE NOW: what is now true that wasn't before.
- ASSUMPTIONS: anything you assumed.
- NEXT: the single next action."""

review_prob = """
Did we meet the goal {goal_var} based on everything you have seen so far in:
EXECUTING output: {output_var}
Situation: {situation_var}

YOUR OUTPUT SHOULD BE "YES" OR "NO" not one word more, without any spaces or spical characters."""
 
worker_probt = """
We haven't achieved our goal: {goal_var}
Take all the data from the past agents {situation_var}
look at the plan: {plan_var}
Current work on the project is {exe1_var}

output the solution to the goal as best you can, only give output that is part of the solution, 
"""
 
def chat(model, system, user, think=True) -> list[str]:
    """One system + one user turn. Returns the assistant's content."""
    resp = requests.post(
        URL + "/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "think": think,
            "stream": False,
        },
    )
    resp.raise_for_status()
    msg = resp.json()["message"]
    if msg.get("thinking"):
        print("Thinking:\n", msg["thinking"], "\n")
    print("Answer:\n", msg["content"], "\n")
    return msg["content"]
 
def models() -> list[str]:
    url_models = URL + "/api/tags"
    print(f"sending request to url: {url_models}")
    
    request = requests.get(url_models)
    names = []
    for m in request.json()["models"]:
        names.append(m["name"])
        #print(f"m is {m}")
    
    return names

def write_text_file(text: str, name: str):
    with open(name, "w") as f:
        f.write(text)

#TODO
#fuctnion to get current, time and run the code, and find out how much time has gone by
def timmer_function():
    pass

#main loop
def main():
    #TODO
    #model picking should be dyamic
    model = "qwen3:14b"
 
    #TODO
    #THIS NEEDS TO BE DYNAMIC, based on user input, add down the line
    # Real, filled-in inputs — NOT a blank template.
    goal = "A basic, fast, multi-user blog web app with user login."
    situation = (
        "- Starting point: empty project; i need you to write and output the code.\n"
        "- Deadline: no hard deadline.\n"
        "- Resources: a few hours; comfortable with Python.\n"
        "- Constraints: keep the stack simple; run locally first."
    )

    #"*" this iterates over the list 
    print("Models:", *models(), sep="\n  ")

    #making plan
    print("\n=== PLANNING ===")
    plan = chat(
        model,
        PLANNER_SYSTEM,
        f"GOAL:\n{goal}\n\nMY SITUATION:\n{situation}",
    )

    #first run
    print("\n=== EXECUTING (Phase 1) ===")
    exe1 = chat(
        model,
        EXECUTOR_SYSTEM,
        f"GOAL:\n{goal}\n\n"
        f"THE PLAN:\n{plan}\n\n"
        f"CONTEXT AND DATA:\n{situation}\n\n"
        f"CURRENT FOCUS: Phase 1\n\n"
        "Execute Phase 1 now and produce the actual deliverable.",
    )

    print("\n===Done EXECUTING (Phase 1) ===")
    print(f"what did we get as output {exe1}")

    #How did the ouput god
    print("\n===How Did We do check: (Phase 1) ===")
    message = review_prob.format(
        goal_var = goal,
        output_var = exe1, 
        situation_var = situation
    )
    #asking the model did we complet the goal
    did_we_do_it = chat(
        model,
        message,
        exe1
    )
    print(f"how did it go output: {did_we_do_it}")

    #regex for yes and no 
    yes_pattern = re.compile(r'^(yes|y|yeah|yep|yup|sure|ok|okay)$', re.IGNORECASE)
    no_pattern = re.compile(r'^(no|n|nah|nope|nay)$', re.IGNORECASE)

    #make the list a string
    string_list = "".join(did_we_do_it)
    counter = 0 #TODO REMOVE THIS

    if yes_pattern.match(string_list):
        #the model returned yes, meaning that you did completed the goal
        pass

    elif no_pattern.match(string_list):
        #the model returned no, meaning we didnt complet the goal
        goal_bool = False
        print("got into the no if, meaning your goal isnt done")
        while(goal_bool !=True):
            message_loop = worker_probt.format(
                goal_var = goal,
                situation_var = situation,
                plan_var = plan,
                exe1_var = exe1
            )

            next_step = chat(
                model,
                message_loop,
                exe1
            )

             #asking the model did we complet the goal
            did_we_do_it = chat(
                model,
                message,
                exe1
            )
            
            #TODO
            #REMOVE THIS
            #REMOVE PRINT LINES LATER, FOR TEST
            print(f"loop data from past step {message_loop}")
            print(f"IN LOOP, DID WE DO IT {did_we_do_it}")

        if yes_pattern.match(string_list):
        #the model returned yes, meaning that you did completed the goal
            goal_bool = True
        
        #TODO
        #remove the print line
        counter = counter + 1 #TODO REMOVE THIS
        print(f"IN LOOP, DOING ANOTHER RUN COUNT {count}")

        pass
    else:
        #the model returned something that didnt match any of the regexs for yes or no
        #dont know what we should do in this case
        pass



    
#TODO
#IMPLENT THIS FUNCTION
#will give a str, and run the string, and return the output
#the string should, should be in python format that can be run
def python_tool():
    pass
 
 
if __name__ == "__main__":
    main()