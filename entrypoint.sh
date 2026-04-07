#!/bin/bash

# start the api
python ice_ai_api.py &
API_PID=$!

# keep container alive; exit with error if the api crashes
wait $API_PID
EXIT_CODE=$?

echo "ice_ai_api.py exited with code $EXIT_CODE"
exit $EXIT_CODE
