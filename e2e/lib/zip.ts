// Reads the file names of a zip archive from its central directory. The export tests need only
// the names, so the suite has no zip library.

const END_OF_CENTRAL_DIRECTORY = 0x06054b50;
const CENTRAL_FILE_HEADER = 0x02014b50;

interface Entry {
  name: string;
}

function entries(archive: Buffer): Entry[] {
  // The end record is at least 22 bytes and can have a comment of up to 65535 bytes.
  const lowest = Math.max(0, archive.length - 22 - 0xffff);
  let end = -1;
  for (let at = archive.length - 22; at >= lowest; at -= 1) {
    if (archive.readUInt32LE(at) === END_OF_CENTRAL_DIRECTORY) {
      end = at;
      break;
    }
  }
  if (end < 0) throw new Error("Not a zip archive: no end of central directory record.");
  const count = archive.readUInt16LE(end + 10);
  let at = archive.readUInt32LE(end + 16);
  const out: Entry[] = [];
  for (let i = 0; i < count; i += 1) {
    if (archive.readUInt32LE(at) !== CENTRAL_FILE_HEADER) {
      throw new Error(`Not a zip archive: bad central directory entry ${i}.`);
    }
    const nameLength = archive.readUInt16LE(at + 28);
    const extraLength = archive.readUInt16LE(at + 30);
    const commentLength = archive.readUInt16LE(at + 32);
    out.push({ name: archive.toString("utf8", at + 46, at + 46 + nameLength) });
    at += 46 + nameLength + extraLength + commentLength;
  }
  return out;
}

function names(archive: Buffer): string[] {
  return entries(archive).map((entry) => entry.name);
}

export default { names };
