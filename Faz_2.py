import os, sys, socket, struct, select, time, signal
import threading
from queue import Queue
from argparse import ArgumentParser

default_timer = time.clock
print_lock = threading.Lock()
stateList = []

ICMP_ECHOREPLY = 0  # Echo reply
ICMP_ECHO = 8  # Echo request
ICMP_MAX_RECV = 2048  # Max size of incoming buffer

MAX_SLEEP = 1000

class Status:
    thisIP = "0.0.0.0"
    pktsSent = 0
    pktsRcvd = 0
    minTime = 999999999
    maxTime = 0
    totTime = 0
    fracLoss = 1.0

def checksum(source_string):
    countTo = (int(len(source_string) / 2)) * 2
    sum = 0
    count = 0

    # Handle bytes in pairs (decoding as short ints)
    loByte = 0
    hiByte = 0
    while count < countTo:
        if (sys.byteorder == "little"):
            loByte = source_string[count]
            hiByte = source_string[count + 1]
        else:
            loByte = source_string[count + 1]
            hiByte = source_string[count]
        sum = sum + (hiByte * 256 + loByte)
        count += 2

    # Handle last byte if applicable (odd-number of bytes)
    # Endianness should be irrelevant in this case
    if countTo < len(source_string):  # Check for odd length
        loByte = source_string[len(source_string) - 1]
        sum += loByte

    sum &= 0xffffffff  # Truncate sum to 32 bits (a variance from ping.c, which
    # uses signed ints, but overflow is unlikely in ping)

    sum = (sum >> 16) + (sum & 0xffff)  # Add high 16 bits to low 16 bits
    sum += (sum >> 16)  # Add carry from above (if any)
    answer = ~sum & 0xffff  # Invert and truncate to 16 bits
    answer = socket.htons(answer)

    return answer

def do_one(destIP, timeout, mySeqNumber, numDataBytes, myStats):
    delay = None

    try:  # One could use UDP here, but it's obscure
        mySocket = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.getprotobyname("icmp"))
    except socket.error as e:
        print("failed. (socket error: '%s')" % e.args[1])
        raise  # raise the original error

    my_ID = os.getpid() & 0xFFFF

    sentTime = send_one_ping(mySocket, destIP, my_ID, mySeqNumber, numDataBytes)
    if sentTime == None:
        mySocket.close()
        return delay

    myStats.pktsSent += 1

    recvTime, dataSize, iphSrcIP, icmpSeqNumber, iphTTL = receive_one_ping(mySocket, my_ID, timeout)

    mySocket.close()

    if recvTime:
        delay = (recvTime - sentTime) * 1000
        with print_lock:
            print("%d bytes from %s: icmp_seq=%d ttl=%d time=%d ms" % (
                dataSize, socket.inet_ntoa(struct.pack("!I", iphSrcIP)), icmpSeqNumber, iphTTL, delay)
                  )
        myStats.pktsRcvd += 1
        myStats.totTime += delay
        if myStats.minTime > delay:
            myStats.minTime = delay
        if myStats.maxTime < delay:
            myStats.maxTime = delay
    else:
        delay = None
        with print_lock:
            print("Request timed out.")

    return delay

def send_one_ping(mySocket, destIP, myID, mySeqNumber, numDataBytes):
    destIP = socket.gethostbyname(destIP)

    # Header is type (8), code (8), checksum (16), id (16), sequence (16)
    myChecksum = 0

    # Make a dummy heder with a 0 checksum.
    header = struct.pack(
        "!BBHHH", ICMP_ECHO, 0, myChecksum, myID, mySeqNumber
    )

    padBytes = []
    startVal = 0x42
    for i in range(startVal, startVal + (numDataBytes)):
        padBytes += [(i & 0xff)]  # Keep chars in the 0-255 range
    data = bytes(padBytes)

    # Calculate the checksum on the data and the dummy header.
    myChecksum = checksum(header + data)

    header = struct.pack(
        "!BBHHH", ICMP_ECHO, 0, myChecksum, myID, mySeqNumber
    )

    packet = header + data

    sendTime = time.time()

    try:
        mySocket.sendto(packet, (destIP, 1))  # Port number is irrelevant for ICMP
    except socket.error as e:
        print("General failure (%s)" % (e.args[1]))
        return

    return sendTime

def receive_one_ping(mySocket, myID, timeout):

    timeLeft = timeout / 1000

    while True:  # Loop while waiting for packet or timeout
        startedSelect = time.time()
        whatReady = select.select([mySocket], [], [], timeLeft)
        howLongInSelect = (time.time() - startedSelect)
        if whatReady[0] == []:  # Timeout
            return None, 0, 0, 0, 0

        timeReceived = time.time()

        recPacket, addr = mySocket.recvfrom(ICMP_MAX_RECV)

        ipHeader = recPacket[:20]
        iphVersion, iphTypeOfSvc, iphLength, \
        iphID, iphFlags, iphTTL, iphProtocol, \
        iphChecksum, iphSrcIP, iphDestIP = struct.unpack(
            "!BBHHHBBHII", ipHeader
        )

        icmpHeader = recPacket[20:28]
        icmpType, icmpCode, icmpChecksum, \
        icmpPacketID, icmpSeqNumber = struct.unpack(
            "!BBHHH", icmpHeader
        )

        if icmpPacketID == myID:  # Our packet
            dataSize = len(recPacket) - 28
            return timeReceived, dataSize, iphSrcIP, icmpSeqNumber, iphTTL

        timeLeft = timeLeft - howLongInSelect
        if timeLeft <= 0:
            return None, 0, 0, 0, 0


def dump_stats(myStats):
    with print_lock:
        print("\n----%s PYTHON PING Statistics----" % (myStats.thisIP))

        if myStats.pktsSent > 0:
            myStats.fracLoss = (myStats.pktsSent - myStats.pktsRcvd) / myStats.pktsSent

        print("%d packets transmitted, %d packets received, %0.1f%% packet loss" % (
            myStats.pktsSent, myStats.pktsRcvd, 100.0 * myStats.fracLoss
        ))

        if myStats.pktsRcvd > 0:
            print("round-trip (ms)  min/avg/max = %d/%0.1f/%d" % (
                myStats.minTime, myStats.totTime / myStats.pktsRcvd, myStats.maxTime
            ))

        print()
    return


def signal_handler(signum, frame):

    for i in stateList:
        dump_stats(i)

    sys.exit(0)

def verbose_ping(hostname, numDataBytes=55):
    myStats = Status()
    stateList.append(myStats)

    mySeqNumber = 0
    try:
        destIP = socket.gethostbyname(hostname)
        with print_lock:
           print("\nPYTHON PING %s (%s): %d data bytes" % (hostname, destIP, numDataBytes))
    except socket.gaierror as e:
        with print_lock:
          print("\nPYTHON PING: Unknown host: %s (%s)" % (hostname, e.args[1]))
          print()
        return

    myStats.thisIP = destIP

    for i in range(5):
        delay = do_one(destIP, timeout, mySeqNumber, numDataBytes, myStats = myStats)
        if delay == None:
            delay = 0
        mySeqNumber += 1

        if (MAX_SLEEP > delay):
            time.sleep((MAX_SLEEP - delay) / 1000)

def threader():
    while True:
        worker = q.get()
        verbose_ping(worker, numDataBytes=PacketSize)
        q.task_done()

q = Queue()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)

    parser = ArgumentParser()
    parser.add_argument('-l', '--List', action='store', dest='finalList',
                        type=str, nargs='+',
                        help="Examples: -l host1 item2, -l host2.... string input type")

    parser.add_argument('--PacketSize', help="Size of packets for ping", type=int)
    parser.add_argument('--timeOut', help="timeout of each port", type=float)

    opts = parser.parse_args()
    if opts.timeOut is not None :
       timeout = opts.timeOut
    else:
        timeout = 1000

    if opts.PacketSize is not None:
       PacketSize = opts.PacketSize

    for i in opts.finalList:
        q.put(i)

    for x in range(8):
        t = threading.Thread(target=threader)
        t.daemon = True
        t.start()

    q.join()
    time.sleep(10)