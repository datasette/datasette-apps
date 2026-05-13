from pluggy import HookspecMarker

hookspec = HookspecMarker("datasette")


@hookspec
def register_app_capabilities(datasette):
    "Return AppCapability objects for datasette-apps."
