import sys,qrcode
if len(sys.argv)!=2 or not sys.argv[1].startswith('https://'):raise SystemExit('Usage: python make_qr.py https://your-live-url/')
qrcode.make(sys.argv[1]).save('competition_qr.png')
print('Saved competition_qr.png')
