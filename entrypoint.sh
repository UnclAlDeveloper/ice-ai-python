#!/bin/bash

# start the api
python ice_ai_api.py &
API_PID=$!

# start the standalone scheduler that fires ebay.py daily between 06:00-07:00 UK time
python scheduler.py &
SCHED_PID=$!

# fail fast: exit as soon as either child dies so the orchestrator (ECS/Copilot)
# restarts the container with a clean state instead of running half-up
wait -n $API_PID $SCHED_PID
EXIT_CODE=$?

# tear down whichever child is still alive before we exit
kill $API_PID $SCHED_PID 2>/dev/null
wait 2>/dev/null

echo "container exiting; first child to die returned $EXIT_CODE"
exit $EXIT_CODE
