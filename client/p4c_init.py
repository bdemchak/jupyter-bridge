import IPython, sys
    
# Install py4cytoscape module into python space
if 'py4cytoscape' not in sys.modules: # Check to see if py4cytoscape already loaded
  # No ... if module name not already defined by user, choose default module name
  if "_PY4CYTOSCAPE" not in globals(): _PY4CYTOSCAPE = 'py4cytoscape'
        
  # If Colab, module is installed at system level. If Notebook, module is in user-space  
  RunningInCOLAB = 'google.colab' in str(get_ipython())
  ModuleSpace = '' if RunningInCOLAB else '--user'
  get_ipython().run_line_magic('run', '-m pip install ' + ModuleSpace + ' ' + _PY4CYTOSCAPE)

import py4cytoscape as p4c

# Start the Jupyter-Bridge to enable communication with Cytoscape on workstation
browser_client_js = p4c.get_browser_client_js(True)
print(f'Loading Javascript client ... {p4c.get_browser_client_channel()} on {p4c.get_jupyter_bridge_url()}')
# Caller must do this part: IPython.display.Javascript(browser_client_js) # Start browser client'