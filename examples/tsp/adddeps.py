# we would prefer a symbolic link, but it does not work on windows
import os
target = os.path.join(os.path.dirname(__file__),
                      '../../opentuner/utils/adddeps.py')
exec(compile(open(target).read(), target, 'exec'), dict(__file__=target))

