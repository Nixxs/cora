from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()
previous_response_id = None

print("Hi im CORA, how can I help you today?\n")

while True:
    # get input from user to prompt the model
    message = input("You: ").strip()

    # validate input 
    if message == "/quit":
        break

    if not message:
        continue

    response = client.responses.create(
        model="gpt-6.1-sol",
        instructions="You are a helpful assistant. Keep replies concise.",
        input=message,
        previous_response_id=previous_response_id,
        store=True,
    )
    
    print(f"\nAI: {response.output_text}\n")
    previous_response_id = response.id