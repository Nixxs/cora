import asyncio
import json
import uuid


async def send_message(process, text, *, recipient=None, group_id=None):
    if (recipient is None) == (group_id is None):
        raise ValueError("Provide either recipient or group_id.")

    params = {"message": text}

    if group_id is not None:
        params["groupId"] = group_id
    else:
        params["recipients"] = [recipient]

    request_id = str(uuid.uuid4())

    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "send",
        "params": params,
    }

    process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
    await process.stdin.drain()

    return request_id

async def receive_messages():
    process = await asyncio.create_subprocess_exec(
        "docker", "compose", "run", "--rm", "-T",
        "signal-cli",
        "-a", "+61427697836",
        "jsonRpc",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    try:
        await send_message(
            process,
            "Hello from Python!",
            recipient="+614XXXXXXXX",
        )

        async for line in process.stdout:
            event = json.loads(line)

            if "id" in event:
                print("Command response:", json.dumps(event, indent=2))
                continue

            envelope = event.get("params", {}).get("envelope", {})

            message = envelope.get("dataMessage")
            outgoing = False

            if message is None:
                message = envelope.get("syncMessage", {}).get("sentMessage")
                outgoing = True

            if message is None:
                continue

            text = message.get("message")
            if text is None:
                continue

            group = message.get("groupInfo", {}).get("groupName")
            group_id = message.get("groupInfo", {}).get("groupId")

            if outgoing:
                sender = "You"
                chat = (
                    group
                    or message.get("destinationNumber")
                    or message.get("destinationUuid")
                    or "Direct chat"
                )
            else:
                sender = (
                    envelope.get("sourceName")
                    or envelope.get("sourceNumber")
                    or envelope.get("sourceUuid")
                )
                chat = group or "Direct chat"

            print(f"[{chat}] {sender}: {text}")

            if group == "NRT":
                await send_message(
                    process,
                    "send test!",
                    group_id=group_id,
                )

        return_code = await process.wait()
        print(f"Receiver exited with code {return_code}")

    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


asyncio.run(receive_messages())