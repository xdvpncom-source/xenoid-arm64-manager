"""Private per-Broker UI reader using Android's exact stock DumpCommand.

Only the JVM is persistent. Each request still creates the stock command,
connects/dumps/disconnects as Android implements it. No input injection or socket.
The separate rare navigation-proof request reads only SystemUI Back through a
fresh cache-bypassing connection in that same serialized JVM.
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time

MAGIC = 0x58554931
MAX_XML = 8 * 1024 * 1024
MAX_NAVIGATION = 16 * 1024
MAIN_CLASS = "org.xenoid.qa.StockUiServer"
JAR_SHA256 = '6290d1500fd1012019556a63c8993c048bccd76e42f0f4c40928fa548f9d12d0'
JAR_GZIP_BASE64 = 'H4sIAAAAAAAC/3V6VVAcDKwuXpwfdynuFHdpkeLuDsXdWWDxUqD44u5W3Iqzi7PY4lDcXYo73J6H+3AeTjKZTDJJZjJfMnmJuhIiEgEcHBzqP/loTNbtNxvECAdnrAAHh/HP883JwtPT2pPTyhqQC8QwT5lPfW/f6NpIzqSH/AeC0kOQrrA/osErySPIIfTSd01/gmJCU6wYx5VpIOhzv+y8TX7XJCynm5TOt+AYHmmttJwUuxg5z/sQG5lQB1Y/+bKdL/S8Of/ZaN/IhSJpndU+PE+2H+Q9S9yveoIzlfkEnOtcHUqLpIP1X5TXponPV6/+7OWfPe0Twpwo3o2P1hmH86iKFGiz1n/3fT0GZ3My+CdM7Z1B2qIrMLwiM+0kDm8FiggOS31o6Q6FiZkJfNuiHcijj0MZzRmmsI0RjFFyECTQVxHF4IA4D2h/MTbvQ6qimeFWPwDxHyT+ikoZfd5FHA3l3qyC4EPUQs0Q6hCDEbomDqnrUVERUiH+5jhuFEJoQLg20of/3Mj/cm8mQ0wg5aHBSBI4qzhAQgAcABVA8ED1F3ETDP+uGIuzhE4B54JwDk+FEITahZJhOGVbQHiIKBf6DRIZAoPHgYfB1cETh4TpFqEXIC+hniLGwwXjrCKLIXTBta1EpcGfhL5C5iFnEN8Q4pANuDcdGR0p0SkMZThfuA2EvA9B2F0timaCXOooc/ATEAYp4imMU+Q7+B2IxeYrBAuiAmHZ1N0shjCFdoY+h/BCdiBd8/pqBQQVcGlwYyGG5pT1lCQfXOBcECVQVtFWkYFoAAIAOoAKgAVAfKBUb0X59iEeTg+RGk4VvhUxAR4GHwxHxhlKiZiOIBrSDWFORIiHZwo1ClFDKP9X/Tm07l83CfAbH/LggxBeMAHID+LmwTinmProzfC5UpL1H0j+VVuEE0SgRpDAXsXIQaaCz/tg+mEdQdkxGj1U05xlCv0GPiCEZZPETdwN3w3jr8Im1SbmX5XNTggvBIwIRgrCiMKmZVNHYkdiDWEtwLWDX0DshpjSIK+iAtEf0P+abNpC8uE3RKpJ69GXkIzRrEJw4KgRz+F7MP8hTf4PE+S/ZH8dNxHMz2SeISLmbPUYJCiM/2BQ2KSDrEFeIeGQYsgspAniCMmGuMLBf7VCRA/xMGdRx6hG+I3w/C/QGXIQ8jWUKcQTogSpC9mo5z0Z6kLELiDRR9VGGAslhJAVkC9hYsLvSJEJoRjj/BsyBDH4jMViNSnhXybo2ohLZd+d5atScCL+4CmDzEW+MzCx41r4o81KkxBVcuL+KsQ6NYJvs+nAPEQWgM8OlTDHcBOewsGEC4fompPX577y1eMvIZ+i3MHpwekhSP/rGwETkU6KIAr+OUTDHFMIs4taCPpG1Qb/AOdGl48jJZ9LGIH1lBGhGEkIu2IdTA8xK3tQ/DtGaJbLZDZdAMsuO67UEBtz/Eyp2KN5WZXaJQ/NJoA93RZca1PrMxV4ZREG2/djmuUOu9pPnVgERbJ2qqfW6Ikt/ikAUC2iD17o3LJrewuwBGECKU8Qkn+5MvzKkxtUY3vF45c0yBueKXrAV6TavVUHts9s+bCFaP2CKenkZ9ZNkovCjs4U86/PnaYW2xWOKDTE7s8qti/g3wV/SmK8tW1TIwWnblPDdoIzhvPHdszGd8ygO35dwwTBEmqTOyKFDxt7UL+Zxq4Wd7nhbKb8jYqVc3T+WkcCmLNJFcC0uFrM8duEn+NaTuvChENNNgG1U85Wf5yISWbrec1oUIar1q3o5YWZQjkCysUwGLMhl2ia4JD2app0iifVVDnVS/5OdioQlDZly2DyuY7VkMPLL3nqVpd5mb1tI0xCWhlRmwmwjDSoUH6h4wUu2vlJldgcdQ6a+3wvb/j5Wn0d/YRlRzgyT+twDtmhf7xhVU7pcAwkMKvIYqbI8fM3+npTnCbss48Ihz1boOGMwFOYYrCGJnmgiqZkoEkptfYjjOUprIToKaLkI3pxEDF77okv27HGmV89unLgr2CJkTiPt1sfjFA1eVarQFn9/Dp5QzkcBbKS4LTmnB66yvw8OA6UQOXa/AJPUJYCvjKGfWUJp8N/FFxGULiOEMwb9AZXA9pnfCnYu4KmpA7aHSFbKbDwePFLDh6O/K9pMMsTr8JtEOdA5RU1yEczemo2hp6jZBBExHJcvhcWGLXuEp2VjndzWAxCITCpsIsuiiHg0NF2bZW59cVduFWacTmxCzUH7xZL4j3T04nT2iNOEJ+mU9yYWBpKScKbIIy9ESssgkw+1dBHbnr2q9Xh5pP6S1Z9wmlVNFRrVVhWhCkdM+abs9oVbl5LL5rlFVM1ot8F1NzQrSdPveM3XMyeVH1WMwVpc5mF4wOiN1/rxk50xLvAXoay78z2GRJc9tx/diUcC+TaYWQlpnSGC5XREvSV6RLVh8MMJtL5yV6Ea1xHDk8NM8G6M0RPpSXpU+/pzeGrHhuTTAklYyNcR2K/CJ4yw3552hqRAVwQ996qFpts98DxLxt1i7NYnrvvuI9KOpRAAULjlAsZ9ZnYF0d4jNMk2IFzi7/TtA2PBuTGlvVTxL/dlkvuFWjexGdxRRuyW3z58R9/uq+65654Cnpj4BebVem4OP655kmpLVbwR3k2srNdQYr+tWnl6E3WJZ22qxBf7mUPB/j+jc8+aENpfwWkLAUfOrJEC12r0zscj3J6afrAFpQpTcc85aH2yLn4X76De4Hiwiporh/89/gIM4Qm/+NBIR3WV/zM1O1kIhavwPV8tWZ6Up0SDjzw9eeof8g1Jd4sETcVNQ+vsW8UGBhpeVoDzARE3CjanAbn1fUn2jZeywaX1XNsmQyJ92Y8iPlqyh9yoKHbVSEllFJQCeI6RcoKxcFbJHmqo0oA/LCAVXGjyvwpV+fbBgIR/BtG1aIjHWW0lWNdGwQliL2oll9jTaAIZfy0Y9dAks8fsmsOoYYGxLBG3LenWKjabBlRciYwIkB3iTsLWKObrFL2uuQo1skoTnzrIz0bRgaQc+lrywfdWeXz980TJ6iXRSiaYZNNOaXMH7nuTrcqSRQIe1z1yrm4x4HeA/NWk5jezU3Fa7v8iXahuX4eZqo9hLqZqD1foSZ+HkZ5q8nJzy0cHr/TE2fHG1HtpA6bTMZ5xn+T8gUzrn/l5S1MH3G4FMnY4ngl8zMaZWO2WlDKbIDVlhnnHt77myqUxIFsaiBqxDWNfkt2353gaYOhfgfSZAvGcr+1xc/fqC7QNF7oVxpll2sLOut/ABdu67yT6jZjfZns3g2ZHvmZalCz7NQ+rMiFBIU0DC8JMJccic8zm9sl7zikPD9YVE7HUo53z1lHpEzYZvZs1PBp7a94prjv70k+/QZScSzONvtiIY+r0QZTnwurzo6/uNubFsMBKa/FVno5Igb80jisl6yX1Eg2gO1Znz32mxsdix1FGz5gKPiJaI1ZFudgjLmNlLRagfaIis9CDFrD9PNwyIwvEjvRHNkXs2zPY/8k0FQN5uTOA3N1m1TKmkQnqw+KFpznNs5KeGVFLzHJTcrEKU+TxfSbvt7cvuDWlcO/Z7x5cgy7d4DDzfCplivbC13Ve14vDV9r+WPnQjcrGifgujpMAFaYFZ8X5dW+XMC3tyrJwhGJCxnH3tbifjh2diKnLeGQJkecClo3x61BwfBAFEL0MU/Na9Vv1sWgT4vQBPUxr3W9c/g7uDVHAX/8JOQgMWNxGVJVktVie+DMSVcd2xwRO3p0IyJh61hW73fJOVCt0VymYC9401T8xoxWh+Mt0mypDwiX5a1t8JoSo9ugEmpZnkHFAjvdhtzzLHXOaub5memF28wtm/fuVdL2o+XIA8uVISsYOcY1XStbt70xuqGzY3iNALd9wuIbTaVQZeRmj15QaeLh0ZTskTnnmSaoUQyqbHRubOSMYKthqqTEAbQ5vMTc4H/RxwnS+0/mSYQ0Em18wHbgcoppwLbexqf9TcbVfIJK2zYAtYrivxj1ckLH7L9X7a7EPe8OhMNbVcSj7Vu6AJcMbPJxPgMzlb7Nr6LR5pT3mFUpFWbRbWG3rO0oOWM6IzWReq3kMNK4pcWJjsj73/I41J/WY18YWa77lWqI+7wgnSl6owR4h8ml2ndHUj7eA/WDmEqrqNH47oOqLEp+re7PabpKDE4WncaDPPpaEJG4Zb0phUYjo5r6Iu/VN80jYkOvE6kVJ44T/glI4A9C+y1b2dSpgcr6BpKkUNR9PztpxfMGFG+BIwkz9bdHhpTIxLyiIH5n7uv29UJYlnmwVgypT9+K9miulooAkodCdSHjF7st+9PCJoJP6zH6x2LANL3v7Le7TaoodQNCCL8Nb7MYrBZ9CcSqpQ8A+U858IuS5nfEW+dbXjpilReZ7yldPbYZwxK3qxz31/mQV3V31QQURwwhMNesJE05r7qb6COuA3WYNUHibC5qWq+gEK6O9+xOhtaEK4NZVcuUHKFgs0aBHbcza6MfomyqcmBoEfpVRK8Bf1V+dRC5vQCXwUHmyOJqV1PEQvaLQO2l4oOkc08cUKD2nggAqzsVss3mqi7zIwial01zGWv2hncJgNmpUhs4R1jN7ydRM/4yYJUn0J3+fB2bNve5J88WV6d5vvop7lZfDcdAePmKhvDTR7mWp6l7ouXd3374t5+XLT3xArCZnm0A8ddlcOJ0B/39eeT3EYi9r6e5NaLlWw1OwQU6C2eM4FklNmpqqTdKj3O/GsTz8IGzHtT1g/2kbnmfhP07zeuJ/HRTmYRHhMl0BSM4STJIjU3Q4AQHnxP/FGOV0d3Bx1b8JjCSwFazB7ha7PlwkYhtknwVEXjdj2FMcHcA0/T5AWRYCiA29mu5yBxitzXyQVm9Ldo+dgrIcrrwnqicmDcFz1e24D0XoLfX4d9KW033Mab/IlxnjJIn8AXxxNFN53Z1Rt0zKFy0qJ8rjccMSQQKj+LMauI8O3/YgK/+W6PXH96DR7tWl9vHKX6wf2sgJdap03yBcIDc/A56/LwLixUWNRyLWK12MNv708UiKlJOYTIFzbNHu5g2OOgnkuH6fNOp09mnL+dPr9Ie5L2Zsol7+evJifebw3gci/k2wFbytt8ne10k+KP5051yxyFhCf5fWYpAioKqvbfKQsekRZu+DFjLH2UKC52I/OmvD8+rqEbOriQAGQHhoovLqNqJ+JGShJGSlcj7ItzcvijDo8/S8lVhrktIwjBRYARaK2MVcU/Ad+XDYZtHlD+2VxZ6ThYXH6oeWIB9YHyYI+SSlj3S+YalkXMX/0nLioO7T5QQ9YSWGNk4IbmFgLviUdUBgz1MD5wTEEI3rqjrGDJajdXmmYrmCPnRfUVkEI6yevqpHAum+f7srsWK3zZtefGQX9R77/dJ0l2CxS0ryKgUZ6q11KwmtKYJKKv6lgIiVvIT0nuL4aEcL91IOXpsv1HiORs8G2O3TiuqV5nsfAj2WO/O8E37gLkBclyCe9WhrXnKHcA+TG8YYvmWeam8NUr4XPFOJvg4Yerwk6xSxVhB4pvnFgqZ2FGSaBxddMhZ9q6R6SXNwmQoKaQ0ILn/KFWsiw1AXbfnrCrDKwgoiaD94lufZ2hka11PeNk22+sYkN0fnTzJpXeDGuGENRdcbLTgNbn1X1djJGW0OXVgotv2ds/Ywc/5Brg8XindnJ+639AMTGJGHDgjOFKSCH1dVn24di/EA+WCkrOTXsQABN/kn/RiRp7IqghvaWdayG78/QTz+MZnxhc9JuxMbM2rEokQ2ez5TcouWb3HdHgcu2YSJhtDJNHTXgELx2B6a8kUJles4eH612GkxWtraPDnK1eFN1Po+5crX7Qq1FEdzi8iBeLsB1GzOktbKr1VsiE9oV5fzqRyv2LWXS57kY6sWM91sYm2ogUn/WjWnbMuKUrNzUauQ7kKHLmvOWXHe7NEPCCmAwOsy5W+Y/orW0BMHdJbQ1U9yAC/hX6YS5hzprkPRO1Sp3X/vD6De+wYzx3To0u8jMz/htffZZxTtPHzjo5nzlW2wymI8z1EkEU8u8Pz2/xt9k89OXeFdRhtNrb5hpWfSgq059jDU1XiazXdaHdba60JqViscBekIcZCWE4yarrJrGXHyrrOAeSIBzJNDXyoN+gXbkjFtClvn3BKJ+pnujTd9mRy2uQLyP87xrk2RNQUGLHYADQ8LUHOIK1dpDnxHvXHEmPtxl7d6nfVnHQwGM0vcVcxVrM8M82qZf+vNgfpdeg16gzQGES13V06grFiooknADs0IBprDFfyhlJ0Sm+YgCfOOOe0kLRtfMeMrftUeDvta19Y/fMBgnGP27lXf2xu815ta20vyqjP/fXiynznUbur5HrNFi8Wr21u1hS1p6uCUxcafOenV950as+65GN3tO0OGypq7zvBmhdxYEWuk+SN9lrBdz3v9k/HaxQmaGNZrDm0tcKKEeRvjdrXBvZy8STArBVAeRfrKjxnrVoaS56SoeyJQaekL9tm98ZEg8Twm06KRrtFwkzfq8wiJnGPSuOhpYKPqo0DZuTbvQgwOWv6L+nDbzzTu3iR2oJFREnr0A9YCmoUAkiHi1af3Sbyhgxpphplv3N4WFulUL+5jxQGaH5dSF7+s/OndJgthQo2mTdXGqAZOXbk4GlwJb5ZNaq1n+q5P7oXMKpr23CVtIWQfhXhjIxy199GPgmQGduul30C44zNcRiYVQRLTFyYP3jO0MZ6JosxGV+JolHlGM0O5G9xJOYntbl1+qn3RBorb9C+YTq2vziVwd20FtjkmGS9ONXIBg3pyjlWmP0NGFVdpnvoJHI8pEY64RiOrlv0zH9RthMWZ5PsNJDGVoHd2/sHFQ7lV7KXRw0J5lCJID2JHfKjvPXIUHdbbsL+ulFtSbjBiVqNjrzGTvHLLSTMfIh7MbVOkXDevbfEFfm2e/+uIcK2h3+jBio7YqFr9c0IYr9+/7GOcIY/3H6tP+yIhvHdoINqWqDVl47fqG11Ys5ly7dJUDNBOTiDM/0Ge5lzbE3Bejhmw6t7U8T05fsJvBh4t4MYNltrdPnyvU2Dgust5CTsGnDpdzAGeMu+fgaslwd+ETV94X7sWoPA8uXfdK4TxodjwOcPzTuAy/LT8tPhGK6lN6tiYqcb95XLbSPnl5csYBAMZUvN+Yb94UKI2n0mcs2rKdvUu7gtO19t5CDhsiT9Fib30L2d5E/bc+tmtT146rdreOLHdHzrG+/kTKHxV1ww4MJKPBJ4IO9l0v2WmwoXSO97cheNnv82SRfFXNNXB76e2kAaC0jp1dOfjnuWyEQaXvXNzPHxduVxcwqM7F3Uch9W/PWgTo6sLFmuwhiOmpGGpv7GLvuQFsL7QZmazgxDKIbZoac0ZMEIi7HnJF/9Qq7PqtWleVnlQJkRV2LDuimGU8ZlQoA8VDCslW8FU9sc589m7tk7h/hFd/OcnOc4rMLfaMA+cNV37lpiZoOGxGYodrp6PmcIPfbH10YEOtaQSnSDjdzbp/s4G4l+9KgTD+5sNS+08Izo/Vhe4R7OCZqNIdsNoJK9naq9dPrnutK8Ha0chpxvm5myynaJr9D4C6/uN2FpH6CzFSif94cK2oVHOwj5xb6D4sL25PaC6lLUge7sXM749PaXnTtZ19dAn33Ln3eUM0WGR91XuR/BJvcaEfoRv/0fmgIHsgJKtAdz2iS1rb+prur0M8+zbfyHhKm0u9Ivt4q8vNu5bvqaWyrAsrd3ZcIutun5EX8hafyquyY6LQuhTE0xZlfXx5d4mmaTrYLZrhCvJk6g1x25Qn85X6F+SMyuPs41keHHpRMP6SdJ/27Cdv/bZpb0fT+8DHxe8R/CT+3GZuRiTdk/poesEsENn6JXUomn23DFj7/7PBIH9pRdBYfq0m27fXCpOqHEpgN/66bu0HiYT+z/SUGMVfwGNf8o9vYvr9dHKsgRqTWuJbnZJ+JQGYAM7uZmcVAF8JelfXGUhi1QwpNPNlurLKpqjY2GNw/l9GI9CT12kBudzrWtKcKMOIKdWsjcTa/vJ7IMP/F8/eTrqB/VydnadO9EeFZedgZkHdXFtFDx+k3uVibsNVFnyw/GXR/yT77FnzP3Iesrg26SgBnSXtc6aY0veJCyp3+WKjdn3WilOfNUG8zUoASRP7RW1OEW61xY8ykpQdxM15ln7VrnITwNTkslrZ2UbAyO6wye+zFJrFk9bjQkP4ZbRRt39CGC7Ha5aJurPvRdL35/Nb+mhb8GLFq81s9gmxqA2E5CUPKTunwfO8N1KTD1Ru8fifzzV8l9nkSPuDi8MJJiSTGHz2BRTMjiaILBvCRNL+Oph1+63yi7rjic5fAJo/GF6l4BtTecUkDISQrq1cHDi9PArPJJBZAmm0dD0l2N/7xw6EIG3RqH3L5SIO54rbMM5pITZLVTe72cNFbG92ElKuD25kZBhEZ9179XRC1mj0rjEDcI9mji1bpJeUZkD+YUYRnFmFyYYqkkBXNwpvnLRJczcqYBTG2w8g9ejuL4etubpSXINa7pOl26vZ9UtriY10jyJfwEmF/iQY613R03BA4lTFMrMzNHR9cHstyLF7gY4hgtxene03pWbBVNZc3NXou1jUZq6ec/88XQKC+kl0+atURZX1zOdoY3hGj3/bYvs5KZXNf0WVnFltWOeM5uLstdZBdVL5YBFrfHyQzLc0sbDmqz3svpCRscRH4CCzns4gnY+TUsJj7SlEj8nbr5nDW4H2H28mo9N38jACowLKZBsQsHqyIM76qWfi4fR62iqRpKik5dR3LpE1kXZySTtq2+o1l13PUwgDESxgACTKnvJNBA+3m704HVt9vrallkYq22FMdkScfQv1p5xVf+mqOj6AaF48lWQ7+asmYBNG94SV5RD6lhuGwroGxXJWizeRsOFHPLlTB2UY+JenTRsFssm6bc6reArvlHf4fVUDqDNUX93xebi7neyG5El6cFn5qLW1dD+yQqnhjE393s//QfLp7PpPukLlTPxrOzcr0qdn3fhxKeJMPEPSLyh5yqF4jvtvCWmT3l/uiiZKzJ723qLUb1hTURD7yERWQ0IMCO8M42vW/QxbKet0zI84MAHcCdoTjLUHDzvtMZNMso/D30TpE6SOjthuys+0zN8ZrYeY42pnlGtkpJz3GFePo6a9XhfPZyvkqwdqjZs4SGgE/h9iIISVuEBmxRGuw7S3P0hTTh0A9e1qhUtQpE/EtEd2b83tz2eBH8baRZFkqkGVOiVKk5rdPIUWttUWssFRQ8wFg60dglPb+jPVdlF1rUYywZ4VCuPXqiHdQ06irXmeOidaUou3cPmKYB7jQqLStoJ3eL8OXkKMrMFurM6VCSRcfGDv74gaMmN5P4Ha9BWpoOKkMrAIpmwpc+0rehSRMhoY6+pb9jiNSEWtkw4yvEDfFIVy71aLOLE3X8dKpA5rdRLywWIYgrZxqmi0xiHmH40fh4Tqoh9rOZLV1up7Odsb6BwAM4Xxitz4gXZ0Ur3aDNWmkVbcUn4MWcF8WIc8RfMLirbUMvjCu9baNBmRUbe5oQSxuhqjInhzetzUNVo0KtamuJr3Wp7o3arapaKmfZ+JKjWLU93WGhjbgXfukFBFC0WGtqNRyrs+N+Zs1SiY8dntY3lfDlEh+I4KmWXT9OND1SZ8yz4Kfi1sanwzWx+FNdYqrAR0WqQj+b+TCSaMxiEkRoUBgA4ClT2+kdedB2ki/VsH/L8TpgiJyhgyoIk2tVarMnkWb9xJEt44FGRxlvZrZg76Fbp5trqRYJ4pOttpcG2JkfQMzvy0id+eun9kd6Hyqq+YQoFCZ2D2JrPrEp9vvIgSorcuBsVbDSzPFiQPGyUF2tYq8RfVFxbtW8Dqi2RwknJfmGXvqkDI+2W5I5q8r+/KWMWPiI19qkC15uifedulug6/CPwryJ3G6jzo5IgXss7Fun5ZlFyaUGPzc7I7X7O3jWFLLXNfbUosK8WnhWvZnet7xz57UK8/pOcLFn/YeXu7wJk0Bmu/WpjVbvsf+iqWb8/UemZvuVuTJMB5LTk3xI5bnsuw8VuK4ChMHEHlbk/qVIOQ3p+YYXnOi3cnQ7H3aeT2ijZXjobOtrRbl58pSH8Ytzd5bSz7RFIlVxdDlLr8YORjYIskl00bkr2LOkrWkksiAEZZZnBBl0AW7sEal25cjdF3EUsXODyd4mS/Hi0dDKcSlGwhhlt4aX6p5KZVNTd866TtfA+xt5J5CjM5Ovao+ERHzZAUzRd3CGrvLBijFz1DXD8+1Lfw5/51aRsamamWmn3/ZAztqLUouTwiC+cIMZccJXvGLvExD0Up8+iTyMwCdeVhOxAVGrYQAVRS8xv5hrYUWmqslZQcM04XrlWMHKsphb+aeg2lK2cuxkmtNPq6FlaDrTEXkQl8+DEykK/gEIY53RSJCQOsGDt2muFMcxf17FnuL02aYVjBXfnDt6RS/EPDQd2jFpRYv1VQAEbmwiWBj+tsNsxPtVV23b3EOGbK9ALPBqn/lT5FOZvg076cQeQ/0POn3CHBsPb6mVyu0zP0TSHKQkbe2jEk9Em9LAT7z2jv8Rm9Kv0zJ179Pa04eTUuaN0lAH+GWNuUSXMm4fNZ+2NXZWKgO7aulSf1KcWxZDW4TpLuLKpzci/LbzmMX8xDwfZuT9nbSseBY/ivPiK8nu0gdcl/gz5IlB4WBfDFx5+A0FKZXM/xxjy3xChia9Gu0AD4c1U7HOml8oCTdvOj3KdUSN/4iy7rhZSd5gi5xZLc81FJYni2WL+GB639/f3+LUrAgviUtxEp0uzVWB7iYoV/wFvDDvbpwZF6tNEwPuFGTkXNLZA8PTfe5xMJMur2rEw63OL84Pl+3pNqr3Hf18RoSWwC3kajk+uyOi1yMd3uYV3wrw31bb+KlMZC3J7VcSlxb6z4PJqZYFbDZ3VADyH3OAl/HnE4HiJtKHV+p6asM/JgIRw8VzPy/ztb7YMcclOtGG8erkly7L7BaIEDnaWIZ3hGMJWc2OTm/SE3bKr0Qw+l5yN2c3r52EUF2dBXV+uLrI65ccx7OhHOhbIyb1dhVrAKs5ix+cy08kFbLFC2i3LpjJ/hA/2is5y6R1R26vHaG8o1gkHKfH1RzCO6wR5+EJ4wU39vRIDAuG+B6+PF2O2GVhJtmlsANu5GZIHSaH7DsUgUIS8eKJCzuzy/FAC9MpRdk/lsWVI7v6QHqTlYRLzW+6wE9Uonydo3+8ndMp03Uy6ZKTG2RSxvuhmF+YeeJlhoah42/FdNLzjw30D2h/j5Yr29BbQZXLtZXRUFXxcPSDn2KVu7nbGg6TsIk6ki3tah/HLjriJazYwaHh1vI2MIvHr8mReaaYn48j4mH4HPjksSrKja60nnwCVGMDIwQRrpR0RnzxQlgt1IyiYTdOLDHoS9DKDNX4f+crbgdKMsgMJcmOsh3BvI6N/fYRt4+OG4sJCXkKk/TeIhrarMbJN6LtH8v+DKqcSIomId3cgYZ/HXYTEcYmZhXN4DJPV0L+rzr50lrNEq44mmFktKg/Uz+ra1OELtqKNl4HtCdc54l5QN7emHMsxrS325aZLVwxEu2VLtGYVzGXXE6QCflsblPEerhXn3m6KdALcEP+mL37yAy6dZl2ZfQIuRZ6vdP6yBGwQC2tnEYiM6VCpLq9wMA20uqwQB6lphx/cuv+AUUYL53BSiOvBphcQIv6u3cpoOj1GJ0+RoRGV4Aq9XUG/6hHZvgBoOejw/Et6WZ/ie8HmV4kjv0lkkEBefb5ygd32qa4C3p2UBJ65o5Qy31xKKCDN0BjW0Zvspb90ut+gP5XY+h0nS5z6gKxaN56mVkPSrJRA3Q6TGRppFnSh+R5IpSw//TkjHz427O5607DADNmvxDu1qTFplvpJ3D0CF974z3UpUcVxN0bft/bin25TvZTGXP4azmoTQ3gyeN0OsD8xMfF1GvCSwkz+Vz5KZ0bmROPOv3bzcnMHHuhk/33/sFKaGFgos3JhGnUdSrxru2hnriyjZUY1fyJBpp3e5Fl2oRxelMvbrsBZ924ZhK6xxG9K936KRXRlmLqarnXbuKurCRYWvOXixfxhPabeMJp9OqWUjySE5YRS9g12uzE0Ddl5V4LB+irdpbywJTLC43YlPrk7KYNwBw9mpn006xV7q5UrGCGigfFOcga1H95icQBCLg8pefwD1iwWmdkg4S4nzMNT1NRB61S0QbzUGcoevShDZNE3J7E02f3TUemRGitMnDxGuHbrxZaaizQk7cix/QC2z1Ore2VkdHjKKi21ipNsxPpCLPlqb6bKW1Z8ZZaJu9EQumg6oyZA5QUFpy7l9MNmKSaWmjaEqnqkwM5C93YWqsGI+LP/Fiks5d0zJgnofkt3+A1M2/uhH4PhvF9FZU+LeASGOzrqiZFZB1JZgRJA+ny05m2fTyy6cE/Mj8SnMyg/bku9Lrri+4ZQw13Oqq0GDTkUSC9ANEld/EJ1ETSWE3Sl9a12oAULUfo3XtC8mmMzvfQSMu2+a0yFU/V6HEYRrzU72VBEL1DzJfvBmfGSnfi4V+5X49zkYRTzEMS8GcLtXP0dPHaR3Sho0eSZKA6I/av0m7y5mQH2GuA/vkRjrJhp6b1kJaX/P7uqsOd7aWG2oXijz5S3EFqBGFcmJcrbdEKp0amvVlofJy8P8rlkpC5TMP35e7Kyxn/yzrr36KowlYZwOdPggLsdHYUm7mJKu2lkuSV6JenyOwrzRoag9pKazgMwUUt3L+LaqxLCns4ZmbEuuqszYwQHeqwP3rtx0V8qWnBum1x5p9e6p3SKSEdIG5+jMvS6JTVNuyui9aEkiHWRfvh8zppNknVyYxzTfJQTYzHCrjuiIXbgE5fnh6xrLhJ2Om7rpDrgKbut2g7a6XbA7lD16Uoz+2gjx09ylPRHnFUlbsX+G3dfaIe298v+y2Se1XWQ9TffwIR3ouYX1tqtp82VdJ3Lv/AWwQDpexgERGv7G6/DJcu2o2X8lY4m1dOvr9GpmM8Uxden6z1Bc3qaqVIOlWK9YKb3cJi5XbcH6nu1m9lbBe04oNpEoKdut9U71hkg4sOYQSvhIdnfsEX0Pe1QI2ncdkjauepsQ7trGCurWfym41ggFv4m5/IbODPeFaRMjtjjTf6v7hTr6zitYffi9BkmVLX4VM5RwIE80r/bJBuu9sGO0ZtRNgGu0ZthNoGe0ZJ4sPezv9r9ACL04c96T/cR2J+fnXteLzQjNB7z0V/Ew3iqGgdBKtZYR4H3w9cj0tWQM4uNwazwvzzHy6MsEwfUzGFh/cL1tNeng3ravvBSTeS6krwCASI//ej2v+nEHi4//22pq6EjPI/Lvh/LPxPlzP+j/X/AOAi4Xj2JgAA'

class UiReaderError(RuntimeError):
    pass


class DumpFailed(UiReaderError):
    """A bounded stock failure, with the framing still synchronized."""


def jar_bytes():
    value = gzip.decompress(base64.b64decode(JAR_GZIP_BASE64))
    if hashlib.sha256(value).hexdigest() != JAR_SHA256:
        raise UiReaderError('ui_reader_artifact_invalid')
    return value


def remaining(deadline):
    if deadline is None:
        return None
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('ui_reader_timeout')
    return value


def proc_record(pid):
    root = Path('/proc') / str(pid)
    text = (root / 'stat').read_text()
    fields = text.rsplit(')', 1)[1].split()
    status = (root / 'status').read_text()
    uid = int(re.search(r'^Uid:\s+(\d+)', status, re.M)[1])
    cmdline = (root / 'cmdline').read_bytes().split(b'\0')
    final = (root / 'stat').read_text().rsplit(')',1)[1].split()
    if final[19] != fields[19]:
        raise ProcessLookupError('process_replaced_during_observation')
    return {'pid': pid, 'state': final[0], 'ppid': int(final[1]), 'starttime': int(final[19]), 'uid': uid,
            'cmdline': cmdline,
            'pidns': os.readlink(str(root / 'ns/pid')),
            'nspid': int(re.search(r'^NSpid:\s+(.+)$', status, re.M)[1].split()[-1])}


class PipeProcess:
    """Framing/timeout/serialization; the owner supplies a fenced remote cleanup."""
    def __init__(self, argv, deadline, stopping, cleanup, prepare_cleanup=None):
        if stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        self.stopping, self.cleanup = stopping, cleanup
        self.prepare_cleanup = prepare_cleanup
        self.lock = threading.Lock()
        self.closed, self.sequence, self.identity = False, 0, None
        self.cleanup_pending = False
        self.stderr_tail = bytearray()
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        self.pump = threading.Thread(target=self._stderr, daemon=True)
        self.pump.start()
        try:
            sequence, status, data = self._reply(deadline)
            parts = data.decode('ascii').split('\t')
            if sequence != 0 or status != 0 or len(parts) != 4 or parts[0] != 'READY':
                raise UiReaderError('ui_reader_startup_protocol')
            self.identity = dict(zip(('pid', 'uid', 'starttime'), map(int, parts[1:])))
        except BaseException as error:
            try:
                self.close(force=True)
            except Exception as cleanup_error:
                error.cleanup_error = cleanup_error
            raise

    def _stderr(self):
        try:
            while True:
                data = os.read(self.process.stderr.fileno(), 4096)
                if not data:
                    return
                self.stderr_tail.extend(data)
                del self.stderr_tail[:-8192]
        except (OSError, ValueError):
            return

    def _read(self, size, deadline, *, shutdown=False):
        value = bytearray()
        with selectors.DefaultSelector() as mux:
            mux.register(self.process.stdout, selectors.EVENT_READ)
            while len(value) < size:
                if not shutdown and self.stopping.is_set():
                    raise UiReaderError('ui_reader_stopping')
                budget = remaining(deadline)
                delay = .2 if budget is None else min(.2, budget)
                if not mux.select(delay):
                    continue
                chunk = os.read(self.process.stdout.fileno(), size-len(value))
                if not chunk:
                    raise UiReaderError('ui_reader_closed')
                value.extend(chunk)
        return bytes(value)

    def _reply(self, deadline, *, shutdown=False, maximum=MAX_XML):
        magic, sequence, status, size = struct.unpack('>IIBI', self._read(13, deadline, shutdown=shutdown))
        if magic != MAGIC or status not in (0, 1, 2) or size > (maximum if status == 0 else 128):
            raise UiReaderError('ui_reader_invalid_frame')
        return sequence, status, self._read(size, deadline, shutdown=shutdown)

    def request(self, windows, deadline):
        return self._request(2 if windows else 0, b'', deadline, MAX_XML)

    def navigation(self, display_id, deadline):
        if type(display_id) is not int or not 0 <= display_id <= 2147483647:
            raise ValueError('ui_reader_navigation_display_invalid')
        return self._request(4, struct.pack('>i', display_id), deadline, MAX_NAVIGATION)

    def _request(self, flags, payload, deadline, maximum):
        with self.lock:
            if self.closed:
                raise UiReaderError('ui_reader_closed')
            self.sequence += 1
            try:
                remaining(deadline)
                self.process.stdin.write(struct.pack('>IIB', MAGIC, self.sequence, flags) + payload)
                self.process.stdin.flush()
                sequence, status, data = self._reply(deadline, maximum=maximum)
                if sequence != self.sequence:
                    raise UiReaderError('ui_reader_sequence_mismatch')
                if status == 1:
                    raise DumpFailed(data.decode('ascii'))
                if status != 0:
                    raise UiReaderError('ui_reader_protocol_rejected')
                return data
            except DumpFailed:
                raise
            except BaseException as error:
                try:
                    self.close(force=True)
                except BaseException as cleanup_error:
                    error.cleanup_error = cleanup_error
                raise

    def close(self, *, force=False):
        if self.closed:
            if self.cleanup_pending:
                self.cleanup(self.identity)
                self.cleanup_pending = False
            return
        self.closed = True
        cleanup_error = None
        try:
            if not force and self.process.poll() is None:
                try:
                    self.sequence += 1
                    self.process.stdin.write(struct.pack('>IIB', MAGIC, self.sequence, 127))
                    self.process.stdin.flush()
                    seq, status, data = self._reply(time.monotonic()+1, shutdown=True)
                    if (seq, status, data) != (self.sequence, 0, b'BYE'):
                        raise UiReaderError('ui_reader_shutdown_protocol')
                    self.process.wait(timeout=1)
                except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    force = True
            if self.prepare_cleanup is not None:
                try:
                    self.prepare_cleanup()
                except BaseException as error:
                    cleanup_error = error
            # Close the submitting transport before sweeping its guest process:
            # a delayed adbd launch must not be submitted after that sweep.
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            try:
                self.cleanup(self.identity)
            except BaseException as error:
                self.cleanup_pending = True
                cleanup_error = error
        finally:
            self.pump.join(timeout=.5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        if cleanup_error:
            raise UiReaderError('ui_reader_cleanup_unconfirmed') from cleanup_error


class PersistentUiReader:
    """Must be created/read while the caller owns the Native instance lock."""
    def __init__(self, manager, container_id, priority, deadline, stopping, *, defer_start=False):
        self.manager, self.container_id = manager, container_id
        self.stopping, self.priority = stopping, priority
        self.deadline = deadline
        self.pipe, self.private_identity = None, None
        self.cleanup_targets = {}
        self._cleanup_binding_verified = False
        self.private = '/data/local/tmp/xenoid-ui-reader-' + os.urandom(16).hex()
        self.claim = os.urandom(16).hex()
        self.directory = self.binding = self.root = self.pidns = None
        self.launch_body = self.host_helper = None
        self.closed = self.started = False
        if not defer_start:
            self.start()

    def start(self):
        if self.closed or self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        if self.started:
            return
        deadline, priority, stopping = self.deadline, self.priority, self.stopping
        self.binding = self._binding()
        if not self.binding['epoch']:
            raise UiReaderError('ui_reader_epoch_unreadable')
        self.root = Path('/proc')/str(self.binding['pid'])/'root'
        self.directory = self.root/self.private.lstrip('/')
        self.pidns = os.readlink('/proc/'+str(self.binding['pid'])+'/ns/pid')
        try:
            result = self._adb('id -u; id -g',deadline)
            values = result.decode('ascii').split()
            if len(values) != 2:
                raise UiReaderError('ui_reader_uid_unreadable')
            self.guest_uid = int(values[0])
            guest_gid = int(values[1])
            if stopping.is_set():
                raise UiReaderError('ui_reader_stopping')
            # Establish the owned directory before any queued shell writes.
            # Cleanup can atomically retire it even if ADB setup starts late.
            self.directory.mkdir(mode=0o700)
            info = self.directory.lstat()
            self.private_identity = (info.st_dev,info.st_ino,info.st_uid)
            os.chown(self.directory,self._host_id(self.guest_uid,'uid_map'),self._host_id(guest_gid,'gid_map'))
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise UiReaderError('ui_reader_private_directory_invalid')
            self.private_identity = (info.st_dev, info.st_ino, info.st_uid)
            self._verify()
            self._adb('set -eu; umask 077; [ ! -e '+shlex.quote(self.private+'/cancelled')+
                ' ]; cat > '+shlex.quote(self.private+'/server.jar')+
                '; chmod 600 '+shlex.quote(self.private+'/server.jar'),deadline,data=jar_bytes())
            self.launch_body = ('set -eu; umask 077; printf "%s %s\\n" "$$" "$(awk \'{print $22}\' /proc/$$/stat)" > '+
                shlex.quote(self.private+'/process.tmp')+'; mv '+shlex.quote(self.private+'/process.tmp')+' '+
                shlex.quote(self.private+'/process')+'; [ ! -e '+shlex.quote(self.private+'/cancelled')+' ]; exec env '+
                shlex.quote('CLASSPATH=/system/framework/uiautomator.jar:'+self.private+'/server.jar')+' '+
                (shlex.join(['nice', '-n', str(priority)])+' ' if priority is not None else '')+
                shlex.join(['app_process', '/system/bin', MAIN_CLASS, self.private]))
            self.pipe = PipeProcess(self._argv(shlex.join(['sh','-c',self.launch_body])),
                                    deadline, stopping, self._terminate, self._capture_cleanup)
            self._helper(self.pipe.identity, required=True)
            self._verify()
            self.started = True
        except BaseException:
            self.close(force=True)
            raise

    def _argv(self, command):
        executable = shutil.which('adb')
        if not executable:
            raise UiReaderError('ui_reader_adb_unavailable')
        return [executable, '-s', self.manager.adb_target, 'shell', '-T', command]

    def _adb(self, command, deadline, data=None):
        if self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        process = subprocess.Popen(self._argv(command),stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        first = True
        try:
            while True:
                if self.stopping.is_set():
                    raise UiReaderError('ui_reader_stopping')
                budget = remaining(deadline)
                try:
                    out,err = process.communicate(input=data if first else None,
                                                   timeout=.2 if budget is None else min(.2,budget))
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode or len(out)>4096 or len(err)>8192:
                raise UiReaderError('ui_reader_setup_failed')
            return out
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def _host_id(self, value, kind):
        for line in (Path('/proc')/str(self.binding['pid'])/kind).read_text().splitlines():
            inside,outside,count = map(int,line.split())
            if inside <= value < inside+count:
                return outside+value-inside
        raise UiReaderError('ui_reader_user_namespace_mapping_missing')

    def _binding(self, *, include_epoch=True):
        budget = remaining(self.deadline) if include_epoch else None
        owned, _ = self.manager._owned_container_record(timeout=5 if budget is None else min(5,budget))
        if not owned or owned.get('Id') != self.container_id or not owned.get('State', {}).get('Running'):
            raise UiReaderError('ui_reader_runtime_changed')
        pid = owned['State']['Pid']
        if include_epoch and self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        probe_deadline = time.monotonic()+2
        if self.deadline is not None:
            probe_deadline = min(probe_deadline,self.deadline)
        return {'pid':pid, 'pid_start':proc_record(pid)['starttime'], 'image':owned.get('Image'),
                'epoch':self.manager._userspace_runtime_epoch(deadline=probe_deadline) if include_epoch else None,
                'instance':self.manager.context.instance_id, 'tag':self.manager.context.resource_tag}

    def _verify(self, *, cleanup=False):
        if not cleanup and self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        current = self._binding(include_epoch=not cleanup)
        keys = ('pid','pid_start','image','instance','tag')
        if any(current[key] != self.binding[key] for key in keys):
            raise UiReaderError('ui_reader_runtime_changed')
        if not cleanup:
            if not current['epoch']:
                raise UiReaderError('ui_reader_epoch_unreadable')
            if current['epoch'] != self.binding['epoch']:
                raise UiReaderError('ui_reader_runtime_changed')

    def _private_ok(self):
        if self.directory is None or self.private_identity is None:
            return False
        info = self.directory.lstat()
        return (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode)==0o700 and
                (info.st_dev,info.st_ino,info.st_uid)==self.private_identity)

    def _matches(self, record, identity=None):
        if record['pidns'] != self.pidns or record['uid'] != self.private_identity[2]:
            return False
        args = record['cmdline']
        command_matches = (MAIN_CLASS.encode() in args and self.private.encode() in args or
                           self.launch_body is not None and self.launch_body.encode() in args)
        if not command_matches:
            return False
        return identity is None or (record['nspid']==identity['pid'] and
                    record['starttime']==identity['starttime'] and identity['uid']==self.guest_uid)

    def _helper(self, identity=None, *, required=False):
        if identity is None and self.directory is not None:
            record_path = self.directory/'process'
            if record_path.exists():
                info = record_path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_size > 128 or info.st_uid != self.private_identity[2]):
                    raise UiReaderError('ui_reader_pid_record_invalid')
                values = record_path.read_text().split()
                if len(values) != 2 or not all(v.isdigit() and int(v)>0 for v in values):
                    raise UiReaderError('ui_reader_pid_record_invalid')
                identity = {'pid':int(values[0]), 'starttime':int(values[1]), 'uid':self.guest_uid}
        candidates = [self.host_helper] if self.host_helper else []
        if not candidates:
            for path in Path('/proc').iterdir():
                if path.name.isdigit():candidates.append(int(path.name))
        found = []
        for pid in candidates:
            try:
                record = proc_record(pid)
                if self._matches(record, identity):found.append(record)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if len(found) > 1:
            raise UiReaderError('ui_reader_process_ambiguous')
        if found:
            self.host_helper = found[0]['pid']
            return found[0]
        if required:
            raise UiReaderError('ui_reader_process_unverified')
        return None

    def _mark_cancelled(self):
        cancel = self.directory/'cancelled'
        try:
            fd = os.open(cancel, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = cancel.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.private_identity[2]:
                raise UiReaderError('ui_reader_cancel_marker_invalid')
        else:
            try:
                os.fchown(fd,self.private_identity[2],self.directory.stat().st_gid)
            finally:
                os.close(fd)

    def _capture_cleanup(self, identity=None):
        if not getattr(self, '_cleanup_binding_verified', False):
            self._verify(cleanup=True)
            self._cleanup_binding_verified = True
        if not self._private_ok():
            raise UiReaderError('ui_reader_private_directory_changed')
        self._mark_cancelled()
        helper = self._helper(identity)
        if helper is None:
            return
        self.cleanup_targets[helper['pid']] = dict(helper, kind='helper')
        # Freeze only the authenticated helper before enumerating descendants.
        # It cannot fork another compiler between this snapshot and pipe close.
        current = self._live_cleanup_target(self.cleanup_targets[helper['pid']])
        if current is not None:
            os.kill(current['pid'],signal.SIGSTOP)
            deadline = time.monotonic()+1
            while True:
                current = self._live_cleanup_target(self.cleanup_targets[helper['pid']])
                if current is None or current.get('state') in ('T','t'):
                    break
                if time.monotonic() >= deadline:
                    os.kill(current['pid'],signal.SIGCONT)
                    raise UiReaderError('ui_reader_freeze_unconfirmed')
                time.sleep(.01)
        processes = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                record = proc_record(int(path.name))
                if record['pidns'] == self.pidns:
                    processes[record['pid']] = record
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        for pid, record in processes.items():
            if not record['cmdline'] or not Path(os.fsdecode(record['cmdline'][0])).name.startswith('dex2oat'):
                continue
            parent, visited = record['ppid'], set()
            while parent in processes and parent not in visited and parent != helper['pid']:
                visited.add(parent)
                parent = processes[parent]['ppid']
            if parent == helper['pid'] and record['uid'] == self.private_identity[2]:
                self.cleanup_targets[pid] = dict(record, kind='compiler')

    def _live_cleanup_target(self, expected):
        try:
            current = proc_record(expected['pid'])
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if current['starttime'] != expected['starttime']:
            return None  # The original process is gone; never signal its reused PID.
        if current.get('state') in ('Z', 'X'):
            return None  # A reaping zombie has no running JVM/compiler to signal.
        if current['uid'] != expected['uid'] or current['pidns'] != expected['pidns']:
            raise UiReaderError('ui_reader_cleanup_identity_changed')
        if expected['kind'] == 'helper':
            if not self._matches(current):
                raise UiReaderError('ui_reader_helper_exec_changed')
        elif current['cmdline'] != expected['cmdline']:
            raise UiReaderError('ui_reader_compiler_exec_changed')
        return current

    def _terminate(self, identity):
        # Capture descendants before the ADB transport closes, and again here.
        # A captured compiler remains owned by PID/starttime even if reparented.
        success = False
        try:
            self._capture_cleanup(identity)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for expected in sorted(self.cleanup_targets.values(),key=lambda r:r['kind']=='helper'):
                    current = self._live_cleanup_target(expected)
                    if current is not None:
                        os.kill(current['pid'],signal.SIGKILL if current.get('state') in ('T','t') else sig)
                end = time.monotonic()+1
                while time.monotonic()<end:
                    if not any(self._live_cleanup_target(r) is not None for r in self.cleanup_targets.values()):
                        success = True
                        return
                    time.sleep(.05)
            raise UiReaderError('ui_reader_process_survived')
        finally:
            if not success:
                # A failed cleanup must not leave an owned process suspended.
                for expected in self.cleanup_targets.values():
                    current = self._live_cleanup_target(expected)
                    if current is not None and current.get('state') in ('T','t'):
                        os.kill(current['pid'],signal.SIGCONT)

    def read(self, manager, windows, deadline):
        if self.closed:
            raise UiReaderError('ui_reader_closed')
        self.manager = manager
        self.deadline = deadline
        try:
            self._verify()
            self._helper(self.pipe.identity, required=True)
            result = self.pipe.request(windows, deadline)
            self._verify()
            self._helper(self.pipe.identity, required=True)
            return result.decode('utf-8')
        except DumpFailed:
            # Stock DumpCommand completed/disconnected but produced no valid XML.
            # Keep this synchronized JVM for the caller's next explicit read.
            raise
        except BaseException:
            self.close(force=True)
            raise

    def navigation(self, manager, display_id, deadline):
        """Rare fresh Back proof; caller already holds the instance lock.

        Never starts a helper or acquires another instance/operation lock. A
        framing/timeout failure uses the same fenced close while that owner is
        held; no Broker.close_ui_reader recursion is involved.
        """
        if self.closed or not self.started or self.pipe is None or self.pipe.closed:
            raise UiReaderError('ui_reader_navigation_unavailable')
        self.manager, self.deadline = manager, deadline
        try:
            self._verify()
            self._helper(self.pipe.identity, required=True)
            data = self.pipe.navigation(display_id, deadline)
            result = json.loads(data.decode('ascii'))
            if not isinstance(result, dict) or result.get('schema') != 'xenoid.nav-node-proof/v1':
                raise UiReaderError('ui_reader_navigation_invalid')
            self._verify()
            self._helper(self.pipe.identity, required=True)
            remaining(deadline)
            return result
        except DumpFailed:
            raise
        except BaseException as error:
            try:
                self.close(force=True)
            except BaseException as cleanup_error:
                error.cleanup_error = cleanup_error
            raise

    def close(self, *, force=False):
        self.closed = True
        if self.private_identity is None and (self.pipe is None or
                self.pipe.closed and not self.pipe.cleanup_pending):
            return
        self._cleanup_binding_verified = False
        error = None
        try:
            if self.pipe is not None:
                self.pipe.close(force=force)
            elif self.private_identity is not None:
                self._terminate(None)
        except BaseException as failure:
            error = failure
        try:
            if self.private_identity is not None:
                # PipeProcess can already be closed after an earlier failed
                # cleanup. Always retry the fenced guest sweep, not just files.
                self._terminate(self.pipe.identity if self.pipe is not None else None)
                if not self._cleanup_binding_verified:
                    self._verify(cleanup=True)
                if not self._private_ok() or self._helper() is not None:
                    raise UiReaderError('ui_reader_cleanup_unconfirmed')
                entries = list(self.directory.iterdir())
                for path in entries:
                    info = path.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or
                        info.st_uid!=self.private_identity[2] or
                        path.name not in ('server.jar','owner','process','process.tmp','cancelled') and not re.fullmatch(r'ui-[A-Za-z0-9_-]+\.xml',path.name)):
                        raise UiReaderError('ui_reader_unknown_cleanup_entry')
                # Remove the launch path atomically before removing the cancel
                # marker. A delayed shell then fails its PID-record redirection
                # under set -e and cannot exec a new compiler/helper afterward.
                retired = self.directory.with_name(self.directory.name+'.retired-'+os.urandom(8).hex())
                if retired.exists():
                    raise UiReaderError('ui_reader_retired_path_exists')
                names = [path.name for path in entries]
                self.directory.rename(retired)
                self.directory = retired
                for name in names:
                    (self.directory/name).unlink()
                self.directory.rmdir()
                self.private_identity = None
                if self.pipe is not None:
                    # The outer fenced retry can complete a sweep that failed
                    # in PipeProcess.close. Retire its pending flag only after
                    # both the owned guest processes and directory are gone.
                    self.pipe.cleanup_pending = False
                error = None  # A retry within this close proved cleanup complete.
        except BaseException as failure:
            error = error or failure
        if error:
            raise UiReaderError('ui_reader_cleanup_unconfirmed') from error
