"""Close Cora through the application's shared shutdown callback."""

from asyncio import sleep
import json


CLOSE_DELAY_SECONDS = 3


SCHEMA = {
	"type": "function",
	"name": "close_app",
	"description": (
		"Close the Cora application only when the person "
		"explicitly requests it. Does not shut down "
		"the computer or close other applications."
        "generally say a natural goodbye or see you "
        "later or refer to something that you were talking "
        "to the person about in the conversation as a good bye or "
        "other relevant message before running this function"
	),
	"parameters": {
		"type": "object",
		"properties": {},
		"required": [],
		"additionalProperties": False,
	},
	"strict": True,
}


def validate_arguments(arguments: dict) -> None:
	if arguments:
		raise ValueError("close_app requires an empty argument object")


class CloseAppTool:
	def __init__(self, get_connection, is_closing, shutdown, write_debug):
		self.get_connection = get_connection
		self.is_closing = is_closing
		self.shutdown = shutdown
		self.write_debug = write_debug
		self.pending = False

	async def handle(self, call_id: str, arguments: dict) -> None:
		if self.is_closing() or self.pending:
			return
		self.pending = True
		self.write_debug(f"close_app requested: call_id={call_id}", "Closing Cora")
		try:
			connection = self.get_connection()
			if connection is not None and not self.is_closing():
				await connection.response.item.create(
					item={
						"type": "function_call_output",
						"call_id": call_id,
						"output": json.dumps({"status": "shutting_down"}),
					}
				)
				# Give the current speech a little time before disconnecting.
				await sleep(CLOSE_DELAY_SECONDS)
			# No response.create: this tool ends the session.
		except Exception as error:
			self.write_debug(f"Close tool result error: {error}", "Tool error")
		finally:
			# Runs separately so the Live reader can process session.closed.
			await self.shutdown()
