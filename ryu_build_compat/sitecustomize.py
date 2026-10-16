from setuptools.command import easy_install

if not hasattr(easy_install, "get_script_args"):
    easy_install.get_script_args = easy_install.ScriptWriter.get_args
