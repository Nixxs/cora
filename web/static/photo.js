// A photo to send to Cora with the next message: chosen with the camera button (on an iPhone,
// Take Photo or Photo Library), shown as a thumbnail in the message box until it's sent or
// removed. It's scaled on the phone to 2048 pixels as JPEG first, so it uploads quickly on
// mobile data; the server scales and checks it again whatever arrives.

const MAX_SIDE = 2048;
const QUALITY = 0.85;

const composer = document.getElementById("composer");
const button = document.getElementById("photo-button");
const picker = document.getElementById("photo-input");
const chip = document.getElementById("photo-chip");
const thumb = document.getElementById("photo-thumb");
const remove = document.getElementById("photo-remove");

let chosen = null; // { base64, url } once ready
let preparing = null;

async function shrink(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", QUALITY));
  if (!blob) throw new Error("Couldn't prepare the photo");
  const base64 = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  return { base64, url: URL.createObjectURL(blob) };
}

function changed() {
  chip.hidden = chosen === null && preparing === null;
  composer.dispatchEvent(new Event("photo-changed"));
}

function clear() {
  chosen = null;
  preparing = null;
  thumb.removeAttribute("src");
  picker.value = "";
  changed();
}

export function hasPhoto() {
  return chosen !== null || preparing !== null;
}

/** The photo for the message being sent ({ base64, url }), or null; the box is cleared. */
export async function takePhoto() {
  const photo = chosen ?? (preparing ? await preparing.catch(() => null) : null);
  chosen = null;
  preparing = null;
  thumb.removeAttribute("src");
  picker.value = "";
  changed();
  return photo;
}

export function setupPhotos(onProblem) {
  button.addEventListener("click", () => picker.click());
  remove.addEventListener("click", clear);
  picker.addEventListener("change", () => {
    const file = picker.files && picker.files[0];
    if (!file) return;
    chosen = null;
    const thisOne = shrink(file);
    preparing = thisOne;
    changed();
    thisOne.then((photo) => {
      if (preparing !== thisOne) return; // replaced or removed meanwhile
      chosen = photo;
      preparing = null;
      thumb.src = photo.url;
      changed();
    }).catch(() => {
      if (preparing !== thisOne) return;
      clear();
      onProblem("That photo couldn't be opened. Try another, or take it again.");
    });
  });
}
