"""Qualified native request copying."""


def copy_tool_call_request(request, tool_call):
    return request.override(tool_call=tool_call)
