// VS Code chooses the first frame with source, even when the actual native stop has
// an instruction pointer. Repair that initial choice once; never fight later clicks.
function nativeFocus(vscode, session, report) {
  let stop;
  const requests = new Map();
  function focus() {
    const item = vscode.debug.activeStackItem;
    if (!stop || stop.used || !stop.frames || item?.session.id !== session.id
        || item.threadId !== stop.threadId) return;
    const index = stop.frames.findIndex((frame) => frame.id === item.frameId);
    if (index < 0) return;
    stop.used = true;
    const top = stop.frames[0];
    if (index === 0 || !top.instructionPointerReference || top.source?.path
        || top.presentationHint === "subtle") return;
    // Earlier source-less glue is subtle. Navigate Up skips it and selects the
    // normal native stop. Do not navigate across any other normal frame.
    if (stop.frames.slice(1, index).some((frame) => frame.presentationHint !== "subtle")) return;
    vscode.commands.executeCommand("workbench.action.debug.callStackUp").catch(report);
  }
  const subscription = vscode.debug.onDidChangeActiveStackItem?.(focus);
  return {
    onWillReceiveMessage(message) {
      if (message.type === "request" && ["continue", "next", "stepIn", "stepOut",
        "stepBack", "reverseContinue", "restart", "disconnect", "terminate"].includes(message.command)) {
        stop = undefined;
      }
      if (message.type === "request" && message.command === "stackTrace") {
        requests.set(message.seq, { stop, args: message.arguments });
      }
    },
    onDidSendMessage(message) {
      if (message.type === "event" && message.event === "stopped") {
        stop = message.body.preserveFocusHint ? undefined : { threadId: message.body.threadId };
      } else if (message.type === "event" && ["continued", "terminated", "exited"].includes(message.event)) {
        stop = undefined;
      } else if (message.type === "response" && message.command === "stackTrace") {
        const request = requests.get(message.request_seq);
        requests.delete(message.request_seq);
        if (message.success && stop && request?.stop === stop
            && request.args.threadId === stop.threadId && !request.args.startFrame
            && message.body?.stackFrames?.length) {
          stop.frames = message.body.stackFrames;
          focus();
        }
      }
    },
    onWillStopSession() {
      stop = undefined;
      requests.clear();
      subscription?.dispose();
    },
  };
}

module.exports = { nativeFocus };
